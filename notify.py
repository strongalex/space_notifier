#!/usr/bin/env python3
"""Email alerts at T-1h and T-5m for Starship, Artemis, and other notable launches.

Data comes from The Space Devs Launch Library 2. GitHub's cron is often late,
so when a launch is close the run stays alive and sleeps until each alert is due.
It re-checks the launch right before sending, which is how scrubs and holds get caught.
"""
import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

API = os.environ.get("LL2_API", "https://ll.thespacedevs.com/2.3.0")
KEYWORDS = [
    k.strip().lower()
    for k in os.environ.get("LAUNCH_KEYWORDS", "starship,artemis,crew,starliner,new glenn").split(",")
    if k.strip()
]
TZ = ZoneInfo(os.environ.get("DISPLAY_TZ", "America/New_York"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

# Ordered largest offset first
ALERTS = [("T-1h", timedelta(hours=1)), ("T-5m", timedelta(minutes=5))]
SEND_STATUSES = {"Go"}
HOLD_STATUSES = {"Hold", "TBD"}

WATCH_HORIZON = timedelta(minutes=90)
WATCH_AFTER = timedelta(minutes=20)  # catches scrubs at T-30s that get posted late
POLL = timedelta(minutes=10)  # free API allows 15 requests/hour
MAX_RUNTIME = timedelta(minutes=115)
SLIP_TOLERANCE = timedelta(minutes=1)
HEARTBEAT_EVERY = timedelta(days=30)  # stops GitHub disabling the schedule after 60 idle days


def utcnow():
    return datetime.now(timezone.utc)


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def fetch_launches():
    url = f"{API}/launches/upcoming/?limit=40&mode=detailed"
    req = urllib.request.Request(url, headers={"User-Agent": "launch-alerts (GitHub Actions)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("results", [])
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"Fetch failed: {e}", file=sys.stderr)
        return None


def is_interesting(launch):
    rocket = ((launch.get("rocket") or {}).get("configuration") or {}).get("full_name") or ""
    mission = (launch.get("mission") or {}).get("name") or ""
    text = f"{launch.get('name', '')} {rocket} {mission}".lower()
    return any(k in text for k in KEYWORDS)


def status_of(launch):
    return (launch.get("status") or {}).get("abbrev", "")


def fmt_time(dt):
    return dt.astimezone(TZ).strftime("%a %b %d, %I:%M %p %Z")


def fmt_delta(td):
    mins = max(0, round(td.total_seconds() / 60))
    if mins >= 60:
        h, m = divmod(mins, 60)
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{mins} min"


def describe(launch):
    pad = launch.get("pad") or {}
    where = (pad.get("location") or {}).get("name") or pad.get("name") or "unknown site"
    lines = [
        launch.get("name", "Unknown launch"),
        f"Time: {fmt_time(parse(launch['net']))}",
        f"Status: {(launch.get('status') or {}).get('name', 'unknown')}",
        f"Site: {where}",
    ]
    vids = [v.get("url") for v in launch.get("vid_urls") or [] if v.get("url")]
    if vids:
        lines.append("Webcast:")
        lines += [f"  {u}" for u in vids[:3]]
    return "\n".join(lines)


def send_email(subject, body):
    user = os.environ["SMTP_USER"]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = os.environ.get("EMAIL_TO", user)
    msg.set_content(body)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT", "465"))) as s:
        s.login(user, os.environ["SMTP_PASS"])
        s.send_message(msg)
    print(f"Sent: {subject}")


def load_state():
    try:
        state = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("launches", {})
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def alert_windows(net):
    # Each alert is valid until the next one takes over, the last one until T-0
    for i, (label, offset) in enumerate(ALERTS):
        end = net - ALERTS[i + 1][1] if i + 1 < len(ALERTS) else net
        yield label, net - offset, end


def process(launches, state, now):
    changed = False
    seen = set()
    for launch in filter(is_interesting, launches):
        lid, net, status = launch["id"], parse(launch["net"]), status_of(launch)
        seen.add(lid)
        entry = state["launches"].get(lid)
        if entry is None:
            entry = {"net": net.isoformat(), "done": {}, "hold_notified": ""}
            state["launches"][lid] = entry
            changed = True

        told = any(entry["done"].values())
        old_net = parse(entry["net"])

        if abs(net - old_net) > SLIP_TOLERANCE:
            if told:
                verb = "delayed" if net > old_net else "moved earlier"
                send_email(
                    f"Launch {verb}: {launch['name']}",
                    f"Was {fmt_time(old_net)}\nNow {fmt_time(net)}\n\n{describe(launch)}",
                )
            entry.update(net=net.isoformat(), done={}, hold_notified="")
            told = False
            changed = True

        if status in HOLD_STATUSES and told and entry["hold_notified"] != status:
            word = "on hold" if status == "Hold" else "scrubbed, new time TBD"
            send_email(f"Launch {word}: {launch['name']}", describe(launch))
            entry["hold_notified"] = status
            changed = True

        for label, start, end in alert_windows(net):
            if label in entry["done"]:
                continue
            if now >= end:
                entry["done"][label] = False  # window missed, don't send stale alert
                changed = True
            elif now >= start and status in SEND_STATUSES:
                send_email(
                    f"{launch['name']} launches in {fmt_delta(net - now)}",
                    describe(launch),
                )
                entry["done"][label] = True
                changed = True

    cutoff = now - timedelta(days=2)
    for lid in list(state["launches"]):
        if lid not in seen and parse(state["launches"][lid]["net"]) < cutoff:
            del state["launches"][lid]
            changed = True
    return changed


def next_wake(launches, state, now):
    wake = None
    for launch in filter(is_interesting, launches):
        net = parse(launch["net"])
        if not (now - WATCH_AFTER <= net <= now + WATCH_HORIZON):
            continue
        done = state["launches"].get(launch["id"], {}).get("done", {})
        cand = now + POLL
        for label, start, _ in alert_windows(net):
            if label not in done and start > now:
                cand = min(cand, start)
        wake = cand if wake is None else min(wake, cand)
    return wake


def heartbeat(state, now):
    last = state.get("heartbeat")
    if last is None or now - parse(last) > HEARTBEAT_EVERY:
        state["heartbeat"] = now.isoformat()
        return True
    return False


def test_email():
    launches = fetch_launches() or []
    upcoming = [describe(l) for l in filter(is_interesting, launches)][:5]
    body = "\n\n".join(upcoming) or "No matching launches in the upcoming list."
    send_email("Launch alerts test", f"Alerts are working. Next matching launches:\n\n{body}")


def main():
    if os.environ.get("TEST_EMAIL") == "1":
        test_email()
        return

    start = utcnow()
    state = load_state()
    if heartbeat(state, start):
        save_state(state)

    last = None
    while True:
        launches = fetch_launches()
        now = utcnow()
        if launches is not None:
            last = launches
            if process(launches, state, now):
                save_state(state)
        elif last is None:
            return  # first fetch failed, next scheduled run will retry

        wake = next_wake(last, state, now)
        if wake is None or wake - start > MAX_RUNTIME:
            return
        delay = max(5.0, (wake - now).total_seconds()) + 2
        print(f"Watching, next check at {fmt_time(now + timedelta(seconds=delay))}")
        time.sleep(delay)


if __name__ == "__main__":
    main()
