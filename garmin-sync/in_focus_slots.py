#!/usr/bin/env python3
"""
Send times for the Garmin In Focus email, and the decision a run makes when
it starts: which send time is it for, and should it wait, send or stop?

GitHub starts timed runs late, sometimes by hours, and now and then not at
all. So the workflow starts many runs around each send time instead of one:

- A run that starts up to 65 minutes BEFORE a send time waits for it and
  sends on the minute (the first such run does; the others stop).
- A run that starts up to 90 minutes AFTER a send time sends straight away,
  unless that send time has already gone out.
- A run that starts at any other time does nothing. In particular a run that
  GitHub starts hours late never sends an email in the middle of the night.

To change the send times, change SEND_TIMES_DUBAI below and the cron lines
in .github/workflows/garmin-in-focus.yml.

    python3 in_focus_slots.py [--slot 07:30]

prints slot / day / phase / goal lines for the workflow to read.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

# Dubai is GMT+4 all year (no summer time).
DUBAI = timezone(timedelta(hours=4))
SEND_TIMES_DUBAI = {"0730": (7, 30), "1400": (14, 0), "2230": (22, 30)}

EARLY = timedelta(minutes=65)  # a run may start this long before and wait
LATE = timedelta(minutes=90)   # after this the send time is skipped
LEAD = timedelta(seconds=40)   # hand over this long before the send time;
#                                the sending job needs about half a minute


def slot_id(text):
    """'07:30', '0730' -> '0730'. Anything else (also 'now' and '') -> None."""
    key = (text or "").strip().replace(":", "")
    return key if key in SEND_TIMES_DUBAI else None


def plan(now, asked=None):
    """Decide what a run starting at `now` (any time zone) should do.

    asked  a slot id when the run was started by hand for one send time.
    Returns {"slot", "day", "phase", "goal"}:
      phase "before"  wait until goal - LEAD, then send
            "due"     send straight away
            "none"    not near any send time: do nothing
    """
    local = now.astimezone(DUBAI)
    for slot, (hour, minute) in SEND_TIMES_DUBAI.items():
        if asked and slot != asked:
            continue
        for day in (local.date(), local.date() - timedelta(days=1)):  # 22:30 can run past midnight
            goal = datetime(day.year, day.month, day.day, hour, minute, tzinfo=DUBAI)
            if goal - EARLY <= local <= goal + LATE:
                phase = "before" if local < goal - LEAD else "due"
                return {"slot": slot, "day": day.isoformat(), "phase": phase, "goal": int(goal.timestamp())}
    return {"slot": asked or "", "day": "", "phase": "none", "goal": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Which In Focus send time is this run for?")
    parser.add_argument("--slot", default="", help="send time asked for by hand, e.g. 07:30")
    parser.add_argument("--now", help="pretend it is this time (ISO, GMT unless an offset is given)")
    args = parser.parse_args(argv)
    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    else:
        now = datetime.now(timezone.utc)
    asked = slot_id(args.slot)
    if args.slot.strip() and not asked:
        print(f"'{args.slot}' is not one of the send times.", file=sys.stderr)
        result = {"slot": "", "day": "", "phase": "none", "goal": 0}
    else:
        result = plan(now, asked)
    for key in ("slot", "day", "phase", "goal"):
        print(f"{key}={result[key]}")
    label = f"{result['slot'][:2]}:{result['slot'][2:]}" if result["slot"] else ""
    if result["phase"] == "before":
        print(f"This run is for the {label} email and will wait for it.", file=sys.stderr)
    elif result["phase"] == "due":
        print(f"This run is for the {label} email, which is due now.", file=sys.stderr)
    else:
        print("No send time is near: nothing to do.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
