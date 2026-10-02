#!/usr/bin/env python3
"""
Export Garmin data for a date range to one JSON file (private use; the file is
never committed). Uses the saved session from garmin_session.py.

    python export_range.py --start 2026-09-21 --end 2026-10-02 --out export.json
"""
import argparse
import json
import sys
from datetime import date, timedelta

import garmin_session

ap = argparse.ArgumentParser()
ap.add_argument("--start", required=True)
ap.add_argument("--end", required=True)
ap.add_argument("--out", required=True)
args = ap.parse_args()

start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
if end < start or (end - start).days > 45:
    sys.exit("Range must be 0-45 days")


def safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        print(f"  skip {fn.__name__}{a[:1]}: {type(e).__name__}", file=sys.stderr)
        return None


def scalars(d):
    """Keep only non-null scalar fields of a dict (drops big time series)."""
    if not isinstance(d, dict):
        return None
    return {k: v for k, v in d.items() if v is not None and isinstance(v, (str, int, float, bool))}


g, seed = garmin_session.login()
out = {"range": {"start": args.start, "end": args.end}, "days": [], "activities": [], "weigh_ins": None}

d = start
while d <= end:
    ds = d.isoformat()
    day = {"date": ds}

    day["stats"] = scalars(safe(g.get_stats, ds))

    sleep = safe(g.get_sleep_data, ds) or {}
    dto = sleep.get("dailySleepDTO") or {}
    s = scalars(dto) or {}
    scores = dto.get("sleepScores") or {}
    s["scores"] = {k: (v.get("value") if isinstance(v, dict) else v) for k, v in scores.items()}
    for k in ("avgOvernightHrv", "hrvStatus", "restingHeartRate", "bodyBatteryChange", "restlessMomentsCount", "avgSkinTempDeviationC"):
        if sleep.get(k) is not None:
            s[k] = sleep[k]
    day["sleep"] = s

    hrv = safe(g.get_hrv_data, ds) or {}
    day["hrv"] = scalars(hrv.get("hrvSummary")) or None
    if day["hrv"] is not None and isinstance((hrv.get("hrvSummary") or {}).get("baseline"), dict):
        day["hrv"]["baseline"] = hrv["hrvSummary"]["baseline"]

    tr = safe(g.get_training_readiness, ds)
    if isinstance(tr, list) and tr:
        day["training_readiness"] = [scalars(x) for x in tr]
    elif isinstance(tr, dict):
        day["training_readiness"] = [scalars(tr)]

    ts = safe(g.get_training_status, ds)
    if isinstance(ts, dict):
        day["training_status"] = {
            "mostRecentVO2Max": ts.get("mostRecentVO2Max"),
            "mostRecentTrainingLoadBalance": ts.get("mostRecentTrainingLoadBalance"),
            "mostRecentTrainingStatus": ts.get("mostRecentTrainingStatus"),
        }

    out["days"].append(day)
    d += timedelta(days=1)

acts = safe(g.get_activities_by_date, args.start, args.end) or []
for a in acts:
    item = scalars(a) or {}
    at = a.get("activityType") or {}
    item["type"] = at.get("typeKey")
    if a.get("summarizedExerciseSets"):
        item["summarizedExerciseSets"] = a["summarizedExerciseSets"]
    if item.get("type") in ("strength_training", "indoor_cardio", "hiit") and a.get("activityId"):
        sets = safe(g.get_activity_exercise_sets, a["activityId"])
        if isinstance(sets, dict) and sets.get("exerciseSets"):
            item["exerciseSets"] = [
                {
                    "type": x.get("setType"),
                    "exercise": ((x.get("exercises") or [{}])[0] or {}).get("name"),
                    "category": ((x.get("exercises") or [{}])[0] or {}).get("category"),
                    "reps": x.get("repetitionCount"),
                    "weight_g": x.get("weight"),
                    "duration_s": x.get("duration"),
                    "start": x.get("startTime"),
                }
                for x in sets["exerciseSets"]
            ]
    out["activities"].append(item)

out["weigh_ins"] = safe(g.get_weigh_ins, args.start, args.end)

with open(args.out, "w") as f:
    json.dump(out, f, indent=1, default=str)

garmin_session.save(g, seed)
print(f"Exported {len(out['days'])} days and {len(out['activities'])} activities")
