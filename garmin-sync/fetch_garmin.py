#!/usr/bin/env python3
"""
Pulls this morning's Garmin stats and writes garmin.json for the V-Taper Coach app.
Runs in GitHub Actions when requested. Credentials come from repo secrets.
Every field is fetched defensively — partial data still produces a valid file.
"""
import json
import os
import sys
from datetime import date, datetime, timezone

from garminconnect import Garmin

EMAIL = os.environ["GARMIN_EMAIL"]
PASSWORD = os.environ["GARMIN_PASSWORD"]

today = date.today().isoformat()
out = {"date": today, "_synced_at": datetime.now(timezone.utc).isoformat()}


def safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        print(f"  skip {fn.__name__}: {e}", file=sys.stderr)
        return None


g = Garmin(EMAIL, PASSWORD)
g.login()

# --- sleep ---
sleep = safe(g.get_sleep_data, today)
if sleep:
    dto = sleep.get("dailySleepDTO") or {}
    scores = dto.get("sleepScores") or {}
    overall = scores.get("overall") or {}
    if overall.get("value") is not None:
        out["sleep_score"] = overall["value"]
    secs = dto.get("sleepTimeSeconds")
    if secs:
        out["sleep_hours"] = round(secs / 3600, 1)

# --- HRV (last night avg + weekly baseline) ---
hrv = safe(g.get_hrv_data, today)
if hrv:
    summary = hrv.get("hrvSummary") or {}
    if summary.get("lastNightAvg") is not None:
        out["hrv"] = summary["lastNightAvg"]
    if summary.get("weeklyAvg") is not None:
        out["hrv_baseline"] = summary["weeklyAvg"]

# --- daily stats: resting HR, body battery, stress ---
stats = safe(g.get_stats, today)
if stats:
    if stats.get("restingHeartRate") is not None:
        out["resting_hr"] = stats["restingHeartRate"]
    bb = stats.get("bodyBatteryMostRecentValue")
    if bb is None:
        bb = stats.get("bodyBatteryHighestValue")
    if bb is not None:
        out["body_battery"] = bb
    if stats.get("averageStressLevel") is not None and stats["averageStressLevel"] >= 0:
        out["stress"] = stats["averageStressLevel"]

# --- weight (Index scale) ---
comp = safe(g.get_body_composition, today)
if comp:
    entries = comp.get("dateWeightList") or []
    if entries:
        w = entries[-1].get("weight")  # grams
        if w:
            out["weight_kg"] = round(w / 1000, 1)

with open("garmin.json", "w") as f:
    json.dump(out, f, indent=2)

print(f"Wrote garmin.json: {out}")
