#!/usr/bin/env python3
"""
Garmin "In Focus" email.

Pulls today's Garmin numbers (heart rate, Body Battery, HRV, sleep in detail,
stress, steps, weight), draws the charts and emails the report through Gmail.
Runs in GitHub Actions three times a day (see .github/workflows/garmin-in-focus.yml).

    python in_focus_report.py                 pull from Garmin and send the email
    python in_focus_report.py --check-mail    only check that Gmail accepts the sign-in
    python in_focus_report.py --from-file sample.json --out-dir preview
                                              build the email from a file of Garmin
                                              responses and write it to a folder
                                              instead of sending (for testing)

The repository is public, so its workflow logs are public too. This script only
ever logs which sections had data. It never prints a health value, an email
address or a password.
"""
import argparse
import io
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import traceback
import warnings
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from html import escape
from pathlib import Path

import in_focus_slots

try:
    from zoneinfo import ZoneInfo

    REPORT_TZ = ZoneInfo(os.environ.get("REPORT_TZ") or "Asia/Dubai")
    REPORT_TZ_NAME = (os.environ.get("REPORT_TZ") or "Asia/Dubai").split("/")[-1].replace("_", " ")
except Exception:  # noqa: BLE001 - no time zone files on this machine
    REPORT_TZ = timezone(timedelta(hours=4))
    REPORT_TZ_NAME = "Dubai"

# Send times in Dubai time. Here they only label the email; when a run sends
# is decided in in_focus_slots.py and the workflow file.
SEND_SLOTS = tuple(in_focus_slots.SEND_TIMES_DUBAI.values())
# A run in the first hours after midnight is the evening email arriving late
# (GitHub can start timed runs very late). It still reports the day that just ended.
LATE_HOURS = 3

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

SECRETS_URL = "https://github.com/frankholck/vtaper/settings/secrets/actions"

# The Garmin calls the report makes. The names are also the keys of the
# "responses" object in a --from-file test file.
CALLS = ("stats", "heart_rates", "body_battery", "hrv", "sleep", "spo2", "respiration", "stress", "steps", "weight")


def log(msg):
    print(f"[in-focus] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Small tolerant readers. Garmin has no official personal API, so every field
# is read defensively: a missing or oddly shaped field means "no data" for
# that item, never a crash.
# --------------------------------------------------------------------------
def as_dict(x):
    return x if isinstance(x, dict) else {}


def as_list(x):
    return x if isinstance(x, list) else []


def num(v):
    """Return v as a float, or None if it is not a usable number."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str):
        try:
            v = float(v.strip())
        except ValueError:
            return None
    if isinstance(v, (int, float)):
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return f
    return None


def pick(*values):
    """First value that is a usable number."""
    for v in values:
        n = num(v)
        if n is not None:
            return n
    return None


def dig(obj, *path):
    for key in path:
        obj = as_dict(obj).get(key)
    return obj


_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?")


def to_utc(v):
    """Garmin sends times as epoch milliseconds or as ISO text. Both are read
    as GMT and returned as a naive datetime (no time zone attached)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            v = float(s)
        else:
            m = _ISO.match(s)
            if not m:
                return None
            try:
                return datetime(*(int(g or 0) for g in m.groups()))
            except ValueError:
                return None
    if isinstance(v, (int, float)):
        secs = v / 1000.0 if abs(v) > 1e11 else float(v)
        try:
            return datetime.fromtimestamp(secs, timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def descriptor_columns(descriptors):
    """Garmin describes array columns as [{key: name, index: n}, ...] with
    slightly different field names per endpoint. Return {name: n}."""
    cols = {}
    for d in as_list(descriptors):
        if not isinstance(d, dict):
            continue
        name = next((v for v in d.values() if isinstance(v, str)), None)
        idx = next((v for v in d.values() if isinstance(v, int) and not isinstance(v, bool)), None)
        if name is not None and idx is not None:
            cols[name.lower()] = idx
    return cols


def rows_series(rows, t_idx, v_idx, offset, keep=None):
    """[[time, ..., value], ...] -> sorted [(local time, value)]."""
    out = []
    for row in as_list(rows):
        if not isinstance(row, (list, tuple)) or len(row) <= max(t_idx, v_idx):
            continue
        t, v = to_utc(row[t_idx]), num(row[v_idx])
        if t is None or v is None or (keep and not keep(v)):
            continue
        out.append((t + offset, v))
    out.sort()
    return out


def dicts_series(items, t_keys, v_keys, offset, keep=None):
    """[{time_key: ..., value_key: ...}, ...] -> sorted [(local time, value)]."""
    out = []
    for item in as_list(items):
        if not isinstance(item, dict):
            continue
        t = next((to_utc(item.get(k)) for k in t_keys if to_utc(item.get(k)) is not None), None)
        v = pick(*(item.get(k) for k in v_keys))
        if t is None or v is None or (keep and not keep(v)):
            continue
        out.append((t + offset, v))
    out.sort()
    return out


def same_day(value, day):
    """True unless Garmin's date text names a different day (a time may be attached)."""
    return not isinstance(value, str) or value.strip()[:10] == day.isoformat()


def within(series, start, end):
    return [(t, v) for t, v in series if start <= t <= end]


def smooth(series, minutes=5):
    """Average minute-by-minute readings into 5-minute steps, so a line of
    whole-number readings is readable instead of a solid block."""
    if len(series) < 3:
        return series
    steps = sorted(b[0] - a[0] for a, b in zip(series, series[1:]))
    if steps[len(steps) // 2] >= timedelta(minutes=minutes - 1):
        return series
    buckets = {}
    for t, v in series:
        start = t - timedelta(minutes=t.minute % minutes, seconds=t.second, microseconds=t.microsecond)
        buckets.setdefault(start, []).append(v)
    half = timedelta(minutes=minutes / 2)
    return [(start + half, sum(vs) / len(vs)) for start, vs in sorted(buckets.items())]


# --------------------------------------------------------------------------
# Wording
# --------------------------------------------------------------------------
def n0(v):
    return f"{int(round(v)):,}"


def n1(v):
    return f"{v:.1f}"


def dur(seconds):
    minutes = int(round(seconds / 60.0))
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} h {m} min"
    if h:
        return f"{h} h"
    return f"{m} min"


def dur_short(seconds):
    minutes = int(round(seconds / 60.0))
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def clock(dt):
    return dt.strftime("%H:%M")


def day_text(d):
    return f"{d:%a} {d.day} {d:%b}"


def nice(key):
    """GOOD -> Good, POSITIVE_BALANCED -> Positive balanced."""
    if not isinstance(key, str) or not key.strip():
        return None
    return key.strip().replace("_", " ").capitalize()


def signed(v, digits=1):
    text = f"{abs(v):.{digits}f}"
    if float(text) == 0:
        return text
    return ("+" if v > 0 else "−") + text


# --------------------------------------------------------------------------
# Turning the Garmin responses into the report's content
# --------------------------------------------------------------------------
def watch_offset(raw, now_utc):
    """How far the watch's clock is from GMT, taken from the data itself so
    chart times read as the watch showed them (also when travelling)."""
    def valid(delta):
        secs = delta.total_seconds()
        return abs(secs) <= 14 * 3600 and secs % 900 == 0

    for name in ("heart_rates", "stress", "spo2", "respiration", "hrv"):
        r = as_dict(raw.get(name))
        g, l = to_utc(r.get("startTimestampGMT")), to_utc(r.get("startTimestampLocal"))
        if g and l and valid(l - g):
            return l - g
    dto = as_dict(dig(raw.get("sleep"), "dailySleepDTO"))
    g, l = to_utc(dto.get("sleepStartTimestampGMT")), to_utc(dto.get("sleepStartTimestampLocal"))
    if g and l and valid(l - g):
        return l - g
    return now_utc.astimezone(REPORT_TZ).utcoffset() or timedelta(hours=4)


def build_heart(raw, ctx):
    hr, stats = as_dict(raw.get("heart_rates")), as_dict(raw.get("stats"))
    cols = descriptor_columns(hr.get("heartRateValueDescriptors"))
    series = rows_series(hr.get("heartRateValues"), cols.get("timestamp", 0), cols.get("heartrate", 1),
                         ctx["offset"], keep=lambda v: 20 <= v <= 250)
    series = within(series, ctx["day_start"], ctx["day_end"])
    values = [v for _, v in series]
    resting = pick(hr.get("restingHeartRate"), stats.get("restingHeartRate"))
    low = pick(hr.get("minHeartRate"), stats.get("minHeartRate"), min(values) if values else None)
    high = pick(hr.get("maxHeartRate"), stats.get("maxHeartRate"), max(values) if values else None)
    week = pick(hr.get("lastSevenDaysAvgRestingHeartRate"), stats.get("lastSevenDaysAvgRestingHeartRate"))
    rows = []
    if resting is not None:
        rows.append(("Resting", f"{n0(resting)} bpm"))
    if week is not None:
        rows.append(("7-day average resting", f"{n0(week)} bpm"))
    if series:
        rows.append((f"Latest ({clock(series[-1][0])})", f"{n0(series[-1][1])} bpm"))
    if low is not None and high is not None:
        rows.append(("Low / high today", f"{n0(low)} / {n0(high)} bpm"))
    return {"has_data": bool(rows or series), "rows": rows, "series": series, "resting": resting, "week": week}


def build_body_battery(raw, ctx):
    stats = as_dict(raw.get("stats"))
    items = [i for i in as_list(raw.get("body_battery")) if isinstance(i, dict)]
    item = next((i for i in items if isinstance(i.get("date"), str) and same_day(i["date"], ctx["day"])),
                items[0] if items else {})

    def read(rows, descriptors):
        cols = descriptor_columns(descriptors)
        first = next((r for r in as_list(rows) if isinstance(r, (list, tuple))), [])
        v_idx = cols.get("bodybatterylevel", 1 if len(first) <= 2 else 2)
        s = rows_series(rows, cols.get("timestamp", 0), v_idx, ctx["offset"], keep=lambda v: 0 <= v <= 100)
        return within(s, ctx["day_start"], ctx["day_end"])

    series = read(item.get("bodyBatteryValuesArray"), item.get("bodyBatteryValueDescriptorDTOList"))
    if not series:  # the stress response carries the same line
        st = as_dict(raw.get("stress"))
        series = read(st.get("bodyBatteryValuesArray"), st.get("bodyBatteryValueDescriptorsDTOList"))
    values = [v for _, v in series]
    current = pick(stats.get("bodyBatteryMostRecentValue"), values[-1] if values else None)
    high = pick(stats.get("bodyBatteryHighestValue"), max(values) if values else None)
    low = pick(stats.get("bodyBatteryLowestValue"), min(values) if values else None)
    charged = pick(item.get("charged"), stats.get("bodyBatteryChargedValue"))
    drained = pick(item.get("drained"), stats.get("bodyBatteryDrainedValue"))
    wake = pick(stats.get("bodyBatteryAtWakeTime"))
    rows = []
    if current is not None:
        rows.append(("Now", n0(current)))
    if high is not None and low is not None:
        rows.append(("High / low today", f"{n0(high)} / {n0(low)}"))
    if wake is not None:
        rows.append(("At wake-up", n0(wake)))
    if charged is not None and drained is not None:
        rows.append(("Charged / drained", f"+{n0(charged)} / −{n0(drained)}"))
    return {"has_data": bool(rows or series), "rows": rows, "series": series, "current": current, "high": high}


def build_hrv(raw, ctx):
    hrv = as_dict(raw.get("hrv"))
    summary = as_dict(hrv.get("hrvSummary"))
    if not same_day(summary.get("calendarDate"), ctx["day"]):  # another night's reading
        summary, hrv = {}, {}
    last, week = pick(summary.get("lastNightAvg")), pick(summary.get("weeklyAvg"))
    high5 = pick(summary.get("lastNight5MinHigh"))
    base = as_dict(summary.get("baseline"))
    b_low, b_high = pick(base.get("balancedLow")), pick(base.get("balancedUpper"))
    status = nice(summary.get("status"))
    readings = dicts_series(hrv.get("hrvReadings"), ("readingTimeGMT",), ("hrvValue",), ctx["offset"], keep=lambda v: v > 0)
    rows = []
    if last is not None:
        rows.append(("Last night average", f"{n0(last)} ms"))
    if week is not None:
        rows.append(("7-day average", f"{n0(week)} ms"))
    if b_low is not None and b_high is not None:
        rows.append(("Your baseline", f"{n0(b_low)}–{n0(b_high)} ms"))
    if status:
        rows.append(("Status", status))
    if high5 is not None:
        rows.append(("Highest 5 minutes", f"{n0(high5)} ms"))
    return {"has_data": bool(rows), "rows": rows, "last": last, "week": week, "status": status,
            "band": (b_low, b_high) if b_low is not None and b_high is not None and b_high > b_low else None,
            "readings": readings}


STAGES = (  # Garmin's activityLevel -> name; order is bottom row to top row in the chart
    (0, "Deep", "deepSleepSeconds"),
    (1, "Light", "lightSleepSeconds"),
    (2, "REM", "remSleepSeconds"),
    (3, "Awake", "awakeSleepSeconds"),
)


def build_sleep(raw, ctx, heart, hrv):
    sleep = as_dict(raw.get("sleep"))
    dto = as_dict(sleep.get("dailySleepDTO"))
    off = ctx["offset"]
    asleep = pick(dto.get("sleepTimeSeconds"))
    wrong_night = not same_day(dto.get("calendarDate"), ctx["day"])
    if not asleep or asleep <= 0 or wrong_night:
        return {"has_data": False, "synced": False, "rows": [], "stages": [], "segments": [], "panels": []}

    start, end = to_utc(dto.get("sleepStartTimestampGMT")), to_utc(dto.get("sleepEndTimestampGMT"))
    segments = []
    for seg in as_list(sleep.get("sleepLevels")):
        if not isinstance(seg, dict):
            continue
        a, b, level = to_utc(seg.get("startGMT")), to_utc(seg.get("endGMT")), num(seg.get("activityLevel"))
        if a and b and b > a and level is not None and int(level) in (0, 1, 2, 3):
            segments.append((a + off, b + off, int(level)))
    segments.sort()
    start = start + off if start else (segments[0][0] if segments else None)
    end = end + off if end else (segments[-1][1] if segments else None)
    if start and end and end <= start:
        start = end = None

    stages = []
    for level, name, key in STAGES:
        secs = pick(dto.get(key))
        if secs is None and segments:
            secs = sum((b - a).total_seconds() for a, b, lv in segments if lv == level)
        if secs is not None:
            stages.append({"level": level, "name": name, "seconds": secs})
    in_bed = sum(s["seconds"] for s in stages)
    for s in stages:
        s["share"] = s["seconds"] / in_bed if in_bed else None

    score = pick(dig(dto, "sleepScores", "overall", "value"))
    quality = nice(dig(dto, "sleepScores", "overall", "qualifierKey"))
    spo2_avg = pick(dto.get("averageSpO2Value"), dig(raw.get("spo2"), "avgSleepSpO2"))
    spo2_low = pick(dto.get("lowestSpO2Value"))
    resp_avg = pick(dto.get("averageRespirationValue"), dig(raw.get("respiration"), "avgSleepRespirationValue"))
    hr_avg = pick(dto.get("avgHeartRate"))
    stress_avg = pick(dto.get("avgSleepStress"))
    bb_change = pick(sleep.get("bodyBatteryChange"))
    awake_count = pick(dto.get("awakeCount"))

    rows = []
    if score is not None:
        rows.append(("Sleep score", n0(score) + (f" ({quality})" if quality else "")))
    rows.append(("Time asleep", dur(asleep)))
    if start and end:
        rows.append(("Asleep / awake at", f"{clock(start)} / {clock(end)}"))
    if awake_count is not None:
        rows.append(("Times awake", n0(awake_count)))
    if hr_avg is not None:
        rows.append(("Average heart rate", f"{n0(hr_avg)} bpm"))
    if spo2_avg is not None:
        rows.append(("Pulse Ox average" + (" / lowest" if spo2_low is not None else ""),
                     f"{n0(spo2_avg)}%" + (f" / {n0(spo2_low)}%" if spo2_low is not None else "")))
    if resp_avg is not None:
        rows.append(("Respiration average", f"{n0(resp_avg)} breaths/min"))
    if stress_avg is not None:
        rows.append(("Stress average", n0(stress_avg)))
    if bb_change is not None:
        rows.append(("Body Battery change", signed(bb_change, 0)))

    # Overnight timelines, each clipped to the time asleep so they line up
    # with the sleep-stage chart above them.
    panels = []
    if start and end:
        def clip(series):
            return within(series, start, end)

        s_hr = clip(dicts_series(sleep.get("sleepHeartRate"), ("startGMT",), ("value",), off, keep=lambda v: 20 <= v <= 250))
        if not s_hr:
            s_hr = clip(heart.get("series") or [])
        s_hrv = clip(dicts_series(sleep.get("hrvData"), ("startGMT",), ("value",), off, keep=lambda v: v > 0))
        if not s_hrv:
            s_hrv = clip(hrv.get("readings") or [])
        s_spo2 = clip(dicts_series(sleep.get("wellnessEpochSPO2DataDTOList"), ("epochTimestamp",), ("spo2Reading",),
                                   off, keep=lambda v: 50 <= v <= 100))
        if not s_spo2:
            sp = as_dict(raw.get("spo2"))
            for key in ("spO2SingleValues", "spO2HourlyAverages"):
                s_spo2 = clip(rows_series(sp.get(key), 0, 1, off, keep=lambda v: 50 <= v <= 100))
                if s_spo2:
                    break
        s_resp = clip(dicts_series(sleep.get("wellnessEpochRespirationDataDTOList"), ("startTimeGMT",),
                                   ("respirationValue",), off, keep=lambda v: 3 <= v <= 60))
        if not s_resp:
            rp = as_dict(raw.get("respiration"))
            cols = descriptor_columns(rp.get("respirationValueDescriptorsDTOList"))
            s_resp = clip(rows_series(rp.get("respirationValuesArray"), cols.get("timestamp", 0),
                                      cols.get("respiration", 1), off, keep=lambda v: 3 <= v <= 60))

        def mean(series):
            return sum(v for _, v in series) / len(series)

        if s_hr:
            panels.append({"key": "heart rate", "title": "Heart rate", "unit": "bpm", "series": s_hr,
                           "figure": n0(pick(hr_avg, mean(s_hr))), "caption": "avg bpm"})
        if s_hrv:
            panels.append({"key": "HRV", "title": "HRV", "unit": "ms", "series": s_hrv, "band": hrv.get("band"),
                           "figure": n0(pick(hrv.get("last"), mean(s_hrv))), "caption": "avg ms"})
        if s_spo2:
            panels.append({"key": "Pulse Ox", "title": "Pulse Ox", "unit": "%", "series": smooth(s_spo2), "ceiling": 100,
                           "figure": n0(pick(spo2_avg, mean(s_spo2))) + "%", "caption": "average"})
        if s_resp:
            panels.append({"key": "respiration", "title": "Respiration", "unit": "breaths/min", "series": s_resp,
                           "figure": n0(pick(resp_avg, mean(s_resp))), "caption": "avg / min"})

    return {"has_data": True, "synced": True, "rows": rows, "stages": stages, "segments": segments,
            "start": start, "end": end, "score": score, "quality": quality, "asleep": asleep,
            "spo2_avg": spo2_avg, "panels": panels}


def build_stress(raw, ctx):
    st, stats = as_dict(raw.get("stress")), as_dict(raw.get("stats"))
    cols = descriptor_columns(st.get("stressValueDescriptorsDTOList"))
    series = rows_series(st.get("stressValuesArray"), cols.get("timestamp", 0), cols.get("stresslevel", 1),
                         ctx["offset"], keep=lambda v: 0 <= v <= 100)  # negative = not measured
    series = within(series, ctx["day_start"], ctx["day_end"])
    values = [v for _, v in series]
    avg = pick(st.get("avgStressLevel"), stats.get("averageStressLevel"))
    if avg is not None and avg < 0:
        avg = None
    if avg is None and values:
        avg = sum(values) / len(values)
    high = pick(st.get("maxStressLevel"), stats.get("maxStressLevel"), max(values) if values else None)
    if high is not None and high < 0:
        high = None
    rows = []
    if avg is not None:
        rows.append(("Average today", n0(avg)))
    if high is not None:
        rows.append(("Highest", n0(high)))
    if series:
        rows.append((f"Latest ({clock(series[-1][0])})", n0(series[-1][1])))
    for label, key in (("Rest", "restStressDuration"), ("Low", "lowStressDuration"),
                       ("Medium", "mediumStressDuration"), ("High", "highStressDuration")):
        secs = pick(stats.get(key))
        if secs is not None and secs >= 0:
            rows.append((f"Time in {label.lower()}", dur(secs)))
    return {"has_data": bool(rows or series), "rows": rows, "series": series, "avg": avg}


def build_steps(raw, ctx):
    stats = as_dict(raw.get("stats"))
    quarter = dicts_series(raw.get("steps"), ("startGMT",), ("steps",), ctx["offset"], keep=lambda v: v >= 0)
    quarter = [(t, v) for t, v in quarter if ctx["day_start"] <= t < ctx["day_start"] + timedelta(days=1)]
    hourly = [0.0] * 24
    for t, v in quarter:
        hourly[t.hour] += v
    total = pick(stats.get("totalSteps"), sum(hourly) if quarter else None)
    goal = pick(stats.get("dailyStepGoal"))
    distance = pick(stats.get("totalDistanceMeters"))
    rows = []
    if total is not None:
        rows.append(("Steps today", n0(total)))
    if goal:
        rows.append(("Goal", n0(goal) + (f" ({n0(100 * total / goal)}% done)" if total is not None else "")))
    if distance:
        rows.append(("Distance", f"{distance / 1000:.1f} km"))
    return {"has_data": bool(rows or quarter), "rows": rows, "hourly": hourly if quarter else None,
            "total": total, "goal": goal}


def build_weight(raw, ctx):
    entries = []
    for e in as_list(dig(raw.get("weight"), "dateWeightList")):
        if not isinstance(e, dict):
            continue
        grams = pick(e.get("weight"))
        if grams is None or grams <= 0:
            continue
        kg = grams / 1000.0 if grams > 1000 else grams
        when_gmt = to_utc(e.get("timestampGMT"))
        when_local = when_gmt + ctx["offset"] if when_gmt else to_utc(e.get("date"))
        cal = e.get("calendarDate") if isinstance(e.get("calendarDate"), str) else None
        try:
            day = date.fromisoformat(cal[:10]) if cal else (when_local.date() if when_local else None)
        except ValueError:
            day = when_local.date() if when_local else None
        if day is None or day > ctx["day"]:
            continue
        entries.append({"day": day, "when": when_local, "kg": kg, "fat": pick(e.get("bodyFat")),
                        "has_clock": when_gmt is not None})
    entries.sort(key=lambda e: (e["day"], e["when"] or datetime.min))
    if not entries:
        return {"has_data": False, "rows": [], "note": "No weigh-in in the last 30 days."}
    latest = entries[-1]
    today = latest["day"] == ctx["day"]
    earlier = [e for e in entries if e["day"] < latest["day"]]
    rows, note = [], None
    if today:
        label = "Today" + (f" ({clock(latest['when'])})" if latest["has_clock"] else "")
    else:
        label = f"Last weigh-in ({day_text(latest['day'])})"
        note = "No weigh-in today yet, so this is the most recent one."
    rows.append((label, f"{n1(latest['kg'])} kg"))
    if earlier:
        prev = earlier[-1]
        rows.append((f"Change since {day_text(prev['day'])}", f"{signed(latest['kg'] - prev['kg'])} kg"))
    if latest["fat"] is not None and latest["fat"] > 0:
        rows.append(("Body fat", f"{n1(latest['fat'])}%"))
    return {"has_data": True, "rows": rows, "note": note, "kg": latest["kg"], "today": today, "day": latest["day"]}


SECTION_ORDER = ("heart rate", "Body Battery", "HRV", "sleep", "stress", "steps", "weight")


def report_day(now_utc):
    """The date the email is about: today in Dubai, or the day that just
    ended if it is only just past midnight there."""
    return (now_utc.astimezone(REPORT_TZ) - timedelta(hours=LATE_HOURS)).date()


def has_day_data(report):
    """True if Garmin had anything for the report's date. Weight does not
    count: the last weigh-in is shown whatever the date."""
    return any(sec.get("has_data") for name, sec in report["sections"].items() if name != "weight")


def build_report(raw, day, now_utc):
    """Everything the email shows, worked out from the raw Garmin responses."""
    raw = raw if isinstance(raw, dict) else {}
    offset = watch_offset(raw, now_utc)
    day_start = datetime(day.year, day.month, day.day)
    ctx = {"day": day, "offset": offset, "day_start": day_start, "day_end": day_start + timedelta(days=1)}

    def guarded(name, fn, *args):
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001 - one odd response must not sink the email
            log(f"{name}: could not be read ({type(e).__name__})")
            return {"has_data": False, "rows": []}

    heart = guarded("heart rate", build_heart, raw, ctx)
    hrv = guarded("HRV", build_hrv, raw, ctx)
    sections = {
        "heart rate": heart,
        "Body Battery": guarded("Body Battery", build_body_battery, raw, ctx),
        "HRV": hrv,
        "sleep": guarded("sleep", build_sleep, raw, ctx, heart, hrv),
        "stress": guarded("stress", build_stress, raw, ctx),
        "steps": guarded("steps", build_steps, raw, ctx),
        "weight": guarded("weight", build_weight, raw, ctx),
    }

    # Where the day charts stop: the next 6-hour mark after the latest reading.
    now_local = now_utc.replace(tzinfo=None) + offset
    latest = max([now_local] + [s["series"][-1][0] for s in sections.values() if s.get("series")])
    hours = (min(latest, ctx["day_end"]) - day_start).total_seconds() / 3600.0
    span = int(min(24, max(6, -(-hours // 6) * 6)))
    ctx["axis_hours"] = span

    local_now = now_utc.astimezone(REPORT_TZ)
    slot = clock(local_now)
    for h, m in SEND_SLOTS:
        target = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
        if timedelta(minutes=-15) <= local_now - target <= timedelta(minutes=50):
            slot = f"{h:02d}:{m:02d}"
    return {"day": day, "ctx": ctx, "sections": sections, "slot": slot, "pulled": local_now,
            "watch_differs": offset != local_now.utcoffset(), "offset": offset}


# --------------------------------------------------------------------------
# Charts (PNG, sized to read on a phone). One measure per chart.
# --------------------------------------------------------------------------
INK, INK2, GRID, AXIS = "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7"
BLUE = "#2a78d6"
BAND = "#ececea"
STAGE_COLOURS = {0: "#2a78d6", 1: "#1baf7a", 2: "#e87ba4", 3: "#eda100"}  # deep, light, REM, awake
FIG_W, DPI = 5.0, 208  # 1040 px wide
# The sleep-stage chart and the overnight panel share these so their time
# axes line up when stacked in the email.
NIGHT_LEFT, NIGHT_RIGHT = 0.125, 0.835
DAY_LEFT, DAY_RIGHT = 0.095, 0.948  # leaves room for the last time label

_RC = {
    "font.family": "DejaVu Sans", "font.size": 10,
    "axes.edgecolor": AXIS, "axes.linewidth": 0.8, "axes.facecolor": "white",
    "axes.spines.top": False, "axes.spines.right": False, "axes.spines.left": False,
    "axes.grid": True, "axes.grid.axis": "y", "axes.axisbelow": True,
    "grid.color": GRID, "grid.linewidth": 0.8, "grid.linestyle": "-",
    "xtick.color": INK2, "ytick.color": INK2, "xtick.labelsize": 10, "ytick.labelsize": 10,
    "xtick.major.size": 0, "ytick.major.size": 0, "xtick.major.pad": 7, "ytick.major.pad": 5,
    "figure.facecolor": "white", "savefig.facecolor": "white",
}


def _plt():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def _png(fig):
    plt = _plt()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI)
    plt.close(fig)
    return buf.getvalue()


def _title(fig, text, unit=None):
    """Bold title at the figure's top-left, unit after it in lighter ink."""
    h = fig.get_figheight()
    y = 1 - 0.13 / h
    t = fig.text(0.012, y, text, fontsize=11.5, fontweight="bold", color=INK, va="top", ha="left")
    if unit:
        fig.canvas.draw()
        right = t.get_window_extent().x1 / (fig.get_figwidth() * fig.dpi)
        fig.text(right + 0.012, y, unit, fontsize=10.5, color=INK2, va="top", ha="left")


def _with_gaps(series):
    """Break the line where the watch recorded nothing for a while."""
    if len(series) < 2:
        return [t for t, _ in series], [v for _, v in series]
    steps = sorted((b[0] - a[0]) for a, b in zip(series, series[1:]))
    limit = max(steps[len(steps) // 2] * 4, timedelta(minutes=10))
    xs, ys, prev = [], [], None
    for t, v in series:
        if prev is not None and t - prev > limit:
            xs.append(prev + (t - prev) / 2)
            ys.append(float("nan"))
        xs.append(t)
        ys.append(v)
        prev = t
    return xs, ys


def _hour_axis(ax, start, hours):
    import matplotlib.dates as mdates

    step = 3 if hours <= 18 else 4
    marks = list(range(0, hours + 1, step))
    ax.set_xlim(mdates.date2num(start), mdates.date2num(start + timedelta(hours=hours)))
    ax.set_xticks([mdates.date2num(start + timedelta(hours=h)) for h in marks])
    ax.set_xticklabels([f"{h:02d}:00" for h in marks])


def _halo():
    from matplotlib import patheffects

    return [patheffects.withStroke(linewidth=3, foreground="white")]


def chart_day_line(series, title, unit, ctx, fixed_scale=False, wash=False, mark_peak=False):
    """One measure across the day as a line, with the latest value labelled."""
    if len(series) < 2:
        return None
    plt = _plt()
    height = 2.35
    with plt.rc_context(_RC):
        fig = plt.figure(figsize=(FIG_W, height), dpi=DPI)
        ax = fig.add_axes([DAY_LEFT, 0.37 / height, DAY_RIGHT - DAY_LEFT, 1 - (0.37 + 0.50) / height])
        _title(fig, title, unit)
        xs, ys = _with_gaps(series)
        ax.plot(xs, ys, color=BLUE, linewidth=1.5, solid_joinstyle="round", solid_capstyle="round", zorder=3)
        values = [v for _, v in series]
        if fixed_scale:
            ax.set_ylim(0, 104)
            ax.set_yticks([0, 25, 50, 75, 100])
        else:
            from matplotlib.ticker import MaxNLocator

            lo, hi = min(values), max(values)
            pad = max((hi - lo) * 0.14, 3)
            ax.set_ylim(lo - pad * 0.6, hi + pad)
            ax.yaxis.set_major_locator(MaxNLocator(nbins=4, integer=True))
        if wash:
            import numpy as np

            ax.fill_between(xs, np.array(ys, dtype=float), ax.get_ylim()[0], color=BLUE, alpha=0.10, linewidth=0, zorder=2)
        _hour_axis(ax, ctx["day_start"], ctx["axis_hours"])

        span = timedelta(hours=ctx["axis_hours"])
        t_last, v_last = series[-1]
        near_edge = (t_last - ctx["day_start"]) / span > 0.90
        ax.scatter([t_last], [v_last], s=38, color=BLUE, edgecolors="white", linewidths=1.6, zorder=5, clip_on=False)
        ax.annotate(n0(v_last), (t_last, v_last), xytext=(-7, 8) if near_edge else (8, 0), textcoords="offset points",
                    ha="right" if near_edge else "left", va="bottom" if near_edge else "center",
                    fontsize=10.5, fontweight="bold", color=INK, zorder=6, path_effects=_halo(), annotation_clip=False)
        if mark_peak:
            t_top, v_top = max(series, key=lambda p: p[1])
            if abs((t_top - t_last) / span) > 0.05:
                ax.scatter([t_top], [v_top], s=38, color=BLUE, edgecolors="white", linewidths=1.6, zorder=5)
                ax.annotate(n0(v_top), (t_top, v_top), xytext=(0, 7), textcoords="offset points", ha="center",
                            va="bottom", fontsize=10.5, fontweight="bold", color=INK, zorder=6, path_effects=_halo())
        return _png(fig)


def chart_steps(hourly, ctx):
    """Steps per hour as thin columns, with the busiest hour labelled."""
    if not hourly or max(hourly) <= 0:
        return None
    plt = _plt()
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MplPath
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    height, hours = 2.35, ctx["axis_hours"]
    with plt.rc_context(_RC):
        fig = plt.figure(figsize=(FIG_W, height), dpi=DPI)
        ax = fig.add_axes([DAY_LEFT, 0.37 / height, DAY_RIGHT - DAY_LEFT, 1 - (0.37 + 0.50) / height])
        _title(fig, "Steps", "per hour")
        top = max(hourly)
        ax.set_xlim(0, hours)
        ax.set_ylim(0, top * 1.22)
        step = 3 if hours <= 18 else 4
        ax.set_xticks(list(range(0, hours + 1, step)))
        ax.set_xticklabels([f"{h:02d}:00" for h in range(0, hours + 1, step)])
        ax.yaxis.set_major_locator(MaxNLocator(nbins=4, integer=True))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v / 1000:g}K" if v >= 1000 else f"{v:g}"))
        fig.canvas.draw()
        box = ax.get_window_extent()
        width = min(0.62, 24 * 1.7 / box.width * hours)  # never fatter than the mark spec
        r_px = 0.034 * DPI
        rx = r_px / box.width * hours
        ry_full = r_px / box.height * (top * 1.22)
        for h in range(min(hours, 24)):
            v = hourly[h]
            if v <= 0:
                continue
            x0, x1 = h + 0.5 - width / 2, h + 0.5 + width / 2
            ry = min(ry_full, v)
            rxx = rx * ry / ry_full
            verts = [(x0, 0), (x0, v - ry), (x0, v), (x0 + rxx, v), (x1 - rxx, v), (x1, v), (x1, v - ry), (x1, 0), (x0, 0)]
            codes = [MplPath.MOVETO, MplPath.LINETO, MplPath.CURVE3, MplPath.CURVE3, MplPath.LINETO,
                     MplPath.CURVE3, MplPath.CURVE3, MplPath.LINETO, MplPath.CLOSEPOLY]
            ax.add_patch(PathPatch(MplPath(verts, codes), facecolor=BLUE, edgecolor="none", zorder=3))
        peak = max(range(min(hours, 24)), key=lambda h: hourly[h])
        ax.annotate(n0(hourly[peak]), (peak + 0.5, hourly[peak]), xytext=(0, 4), textcoords="offset points",
                    ha="center", va="bottom", fontsize=10.5, fontweight="bold", color=INK)
        ax.spines["bottom"].set_color(AXIS)
        return _png(fig)


def _night_axis(ax, start, end):
    import matplotlib.dates as mdates

    ax.set_xlim(mdates.date2num(start), mdates.date2num(end))
    hours = (end - start).total_seconds() / 3600.0
    step = 1 if hours <= 5 else 2
    first = start.replace(minute=0, second=0, microsecond=0)
    marks = []
    t = first
    while t <= end:
        if t >= start and (t.hour % step == 0):
            marks.append(t)
        t += timedelta(hours=1)
    ax.set_xticks([mdates.date2num(m) for m in marks])
    ax.set_xticklabels([clock(m) for m in marks])


def chart_sleep_stages(sleep):
    """The night as a timeline: one row per sleep stage, time in each at the right."""
    segments, start, end = sleep.get("segments"), sleep.get("start"), sleep.get("end")
    if not segments or not start or not end:
        return None
    plt = _plt()
    import matplotlib.dates as mdates

    height = 2.25
    with plt.rc_context(_RC):
        fig = plt.figure(figsize=(FIG_W, height), dpi=DPI)
        ax = fig.add_axes([NIGHT_LEFT, 0.37 / height, NIGHT_RIGHT - NIGHT_LEFT, 1 - (0.37 + 0.46) / height])
        _title(fig, "Sleep stages")
        for a, b, level in segments:
            a, b = max(a, start), min(b, end)
            if b <= a:
                continue
            x = mdates.date2num(a)
            ax.broken_barh([(x, mdates.date2num(b) - x)], (level - 0.31, 0.62), facecolors=STAGE_COLOURS[level],
                           linewidth=0, zorder=3)
        ax.set_ylim(-0.62, 3.62)
        ax.set_yticks([lv for lv, _, _ in STAGES])
        ax.set_yticklabels([name for _, name, _ in STAGES])
        ax.tick_params(axis="y", labelcolor=INK)
        _night_axis(ax, start, end)
        by_level = {s["level"]: s for s in sleep.get("stages") or []}
        for level, _, _ in STAGES:
            if level in by_level:
                ax.text(1.025, level, dur_short(by_level[level]["seconds"]), transform=ax.get_yaxis_transform(),
                        ha="left", va="center", fontsize=10, fontweight="bold", color=INK)
        return _png(fig)


def chart_overnight(sleep):
    """Sleeping heart rate, HRV, Pulse Ox and respiration stacked on one time axis."""
    panels, start, end = sleep.get("panels") or [], sleep.get("start"), sleep.get("end")
    panels = [p for p in panels if len(p["series"]) >= 2]
    if not panels or not start or not end:
        return None
    plt = _plt()
    from matplotlib.ticker import MaxNLocator

    row, head, foot = 1.02, 0.66, 0.36
    height = head + foot + row * len(panels) + 0.30 * (len(panels) - 1)
    with plt.rc_context(_RC):
        fig = plt.figure(figsize=(FIG_W, height), dpi=DPI)
        _title(fig, "Through the night")
        for i, p in enumerate(panels):
            top = height - head - i * (row + 0.30)
            ax = fig.add_axes([NIGHT_LEFT, (top - row + 0.17) / height, NIGHT_RIGHT - NIGHT_LEFT, (row - 0.17) / height])
            values = [v for _, v in p["series"]]
            lo, hi = min(values), max(values)
            band = p.get("band")
            if band:
                ax.axhspan(band[0], band[1], color=BAND, linewidth=0, zorder=1)
                lo, hi = min(lo, band[0]), max(hi, band[1])
            pad = max((hi - lo) * 0.15, 1)
            ceiling = p.get("ceiling")
            ax.set_ylim(lo - pad, min(hi + pad, ceiling + 0.6) if ceiling else hi + pad)
            ax.yaxis.set_major_locator(MaxNLocator(nbins=3, integer=True))
            xs, ys = _with_gaps(p["series"])
            ax.plot(xs, ys, color=BLUE, linewidth=1.4, solid_joinstyle="round", solid_capstyle="round", zorder=3)
            _night_axis(ax, start, end)
            if i < len(panels) - 1:
                ax.set_xticklabels([])
            ax.text(0, 1.06, p["title"], transform=ax.transAxes, ha="left", va="bottom", fontsize=10,
                    fontweight="bold", color=INK)
            if band:
                ax.text(0.995, 1.06, "grey band = your baseline", transform=ax.transAxes, ha="right", va="bottom",
                        fontsize=9, color=INK2)
            ax.text(1.03, 0.60, p["figure"], transform=ax.transAxes, ha="left", va="center", fontsize=11,
                    fontweight="bold", color=INK)
            ax.text(1.03, 0.28, p["caption"], transform=ax.transAxes, ha="left", va="center", fontsize=9, color=INK2)
        return _png(fig)


def make_charts(report):
    """Return {name: png bytes} for the charts that have enough data."""
    s, ctx = report["sections"], report["ctx"]
    jobs = (
        ("heart_rate", lambda: chart_day_line(s["heart rate"].get("series") or [], "Heart rate", "bpm", ctx, mark_peak=True)),
        ("body_battery", lambda: chart_day_line(s["Body Battery"].get("series") or [], "Body Battery", None, ctx,
                                                fixed_scale=True, wash=True)),
        ("sleep_stages", lambda: chart_sleep_stages(s["sleep"])),
        ("overnight", lambda: chart_overnight(s["sleep"])),
        ("stress", lambda: chart_day_line(s["stress"].get("series") or [], "Stress", "0–100", ctx,
                                          fixed_scale=True, wash=True)),
        ("steps", lambda: chart_steps(s["steps"].get("hourly"), ctx)),
    )
    charts = {}
    for name, job in jobs:
        try:
            png = job()
        except Exception as e:  # noqa: BLE001 - a chart that fails is left out, the numbers still go
            log(f"chart {name}: could not be drawn ({type(e).__name__})")
            png = None
        if png:
            charts[name] = png
    return charts


# --------------------------------------------------------------------------
# The email
# --------------------------------------------------------------------------
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
ALT = {
    "heart_rate": "Heart rate through the day",
    "body_battery": "Body Battery through the day",
    "sleep_stages": "Sleep stages through the night",
    "overnight": "Heart rate, HRV, Pulse Ox and respiration through the night",
    "stress": "Stress through the day",
    "steps": "Steps per hour",
}
SECTION_TITLES = {"heart rate": "Heart rate", "Body Battery": "Body Battery", "HRV": "HRV", "sleep": "Sleep",
                  "stress": "Stress", "steps": "Steps", "weight": "Weight"}
SECTION_CHARTS = {"heart rate": ("heart_rate",), "Body Battery": ("body_battery",), "sleep": ("sleep_stages", "overnight"),
                  "stress": ("stress",), "steps": ("steps",)}


def headline_tiles(report):
    s = report["sections"]
    heart, bb, hrv, sleep, stress, steps, weight = (s[k] for k in SECTION_ORDER)
    tiles = []

    def add(label, value, unit="", sub=""):
        tiles.append({"label": label, "value": value, "unit": unit, "sub": sub})

    if heart.get("resting") is not None:
        add("Resting heart rate", n0(heart["resting"]), "bpm",
            f"7-day average {n0(heart['week'])}" if heart.get("week") is not None else "")
    else:
        add("Resting heart rate", "–", sub="no data")
    if bb.get("current") is not None:
        add("Body Battery", n0(bb["current"]), sub=f"high today {n0(bb['high'])}" if bb.get("high") is not None else "")
    else:
        add("Body Battery", "–", sub="no data")
    if hrv.get("last") is not None:
        add("HRV last night", n0(hrv["last"]), "ms", hrv.get("status") or "")
    else:
        add("HRV last night", "–", sub="not synced yet")
    if sleep.get("score") is not None:
        add("Sleep score", n0(sleep["score"]), sub=" · ".join(x for x in (sleep.get("quality"), dur(sleep["asleep"])) if x))
    elif sleep.get("has_data"):
        add("Sleep", dur(sleep["asleep"]))
    else:
        add("Sleep score", "–", sub="not synced yet")
    if stress.get("avg") is not None:
        add("Stress", n0(stress["avg"]), sub="average today")
    else:
        add("Stress", "–", sub="no data")
    if steps.get("total") is not None:
        add("Steps", n0(steps["total"]), sub=f"goal {n0(steps['goal'])}" if steps.get("goal") else "")
    else:
        add("Steps", "–", sub="no data")
    if sleep.get("spo2_avg") is not None:
        add("Pulse Ox", n0(sleep["spo2_avg"]) + "%", sub="average in sleep")
    else:
        add("Pulse Ox", "–", sub="no data")
    if weight.get("kg") is not None:
        add("Weight", n1(weight["kg"]), "kg", "today" if weight.get("today") else day_text(weight["day"]))
    else:
        add("Weight", "–", sub="no weigh-in")
    return tiles


def section_notes(name, sec):
    notes = []
    if name == "sleep" and not sec.get("has_data"):
        notes.append("Last night's sleep has not synced from the watch yet.")
    elif name == "HRV" and not sec.get("has_data"):
        notes.append("Last night's HRV has not synced from the watch yet.")
    elif name == "HRV" and sec.get("has_data"):
        notes.append("The HRV line through the night is in the Sleep section.")
    elif name == "weight" and sec.get("note"):
        notes.append(sec["note"])
    elif not sec.get("has_data"):
        notes.append("No data from Garmin for this yet.")
    return notes


def instead_note(report):
    wanted, day = report["instead_of"], report["day"]
    return (f"Nothing has synced for {wanted:%A} {wanted.day} {wanted:%B} yet, "
            f"so this is {day:%A} {day.day} {day:%B}.")


def render_html(report, charts, image_src):
    """image_src(name) -> the src for that chart (cid: when emailing, a file name in a preview)."""
    e = escape
    day = report["day"]
    out = []
    out.append('<!DOCTYPE html><html><head><meta charset="utf-8">'
               '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
               '<body style="margin:0;padding:0;background:#ffffff;">')
    out.append(f'<div style="max-width:620px;margin:0 auto;padding:14px 14px 28px;font-family:{FONT};'
               'color:#000000;font-size:15px;line-height:1.45;">')
    out.append('<div style="font-size:22px;font-weight:700;color:#000000;">Garmin In Focus</div>')
    pulled = f"{day:%A} {day.day} {day:%B %Y} · pulled {clock(report['pulled'])} {REPORT_TZ_NAME} time"
    out.append(f'<div style="font-size:14px;color:#000000;margin-top:2px;">{e(pulled)}</div>')
    if report.get("instead_of"):
        out.append(f'<div style="font-size:15px;color:#000000;margin-top:10px;">{e(instead_note(report))}</div>')

    tiles = headline_tiles(report)
    out.append('<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
               'style="border-collapse:collapse;margin-top:14px;">')
    for i in range(0, len(tiles), 2):
        out.append("<tr>")
        for t in tiles[i:i + 2]:
            unit = f' <span style="font-size:14px;font-weight:400;">{e(t["unit"])}</span>' if t["unit"] else ""
            sub = f'<div style="font-size:13px;color:#000000;">{e(t["sub"])}</div>' if t["sub"] else ""
            out.append('<td width="50%" style="border:1px solid #c9c9c9;padding:9px 11px;vertical-align:top;">'
                       f'<div style="font-size:13px;color:#000000;">{e(t["label"])}</div>'
                       f'<div style="font-size:25px;font-weight:700;line-height:1.25;color:#000000;">{e(t["value"])}{unit}</div>'
                       f"{sub}</td>")
        out.append("</tr>")
    out.append("</table>")

    for name in SECTION_ORDER:
        sec = report["sections"][name]
        out.append('<div style="border-top:2px solid #000000;margin-top:28px;padding-top:9px;font-size:18px;'
                   f'font-weight:700;color:#000000;">{e(SECTION_TITLES[name])}</div>')
        if sec.get("rows"):
            out.append('<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
                       'style="border-collapse:collapse;margin-top:6px;">')
            for label, value in sec["rows"]:
                out.append('<tr><td style="padding:5px 0;border-bottom:1px solid #dddddd;font-size:15px;color:#000000;">'
                           f'{e(label)}</td><td align="right" style="padding:5px 0;border-bottom:1px solid #dddddd;'
                           f'font-size:15px;font-weight:700;color:#000000;white-space:nowrap;">{e(value)}</td></tr>')
            out.append("</table>")
        for note in section_notes(name, sec):
            out.append(f'<div style="margin-top:8px;font-size:15px;color:#000000;">{e(note)}</div>')
        if name == "sleep" and sec.get("stages"):
            out.append('<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
                       'style="border-collapse:collapse;margin-top:14px;">')
            for st in reversed(sec["stages"]):  # awake first, like the chart reads top to bottom
                share = f"{round(100 * st['share'])}%" if st.get("share") is not None else ""
                out.append('<tr><td style="padding:5px 0;border-bottom:1px solid #dddddd;font-size:15px;color:#000000;">'
                           f'<span style="display:inline-block;width:11px;height:11px;background:{STAGE_COLOURS[st["level"]]};'
                           f'margin-right:8px;"></span>{e(st["name"])}</td>'
                           '<td align="right" style="padding:5px 0;border-bottom:1px solid #dddddd;font-size:15px;'
                           f'color:#000000;white-space:nowrap;">{e(share)}</td>'
                           '<td align="right" width="96" style="padding:5px 0;border-bottom:1px solid #dddddd;font-size:15px;'
                           f'font-weight:700;color:#000000;white-space:nowrap;">{e(dur(st["seconds"]))}</td></tr>')
            out.append("</table>")
        for chart in SECTION_CHARTS.get(name, ()):
            if chart in charts:
                out.append(f'<img src="{e(image_src(chart))}" width="592" alt="{e(ALT[chart])}" '
                           'style="display:block;width:100%;max-width:592px;height:auto;border:0;margin-top:14px;">')

    foot = "Sent automatically from the vtaper repository on GitHub. Chart times are the watch's own clock"
    if report["watch_differs"]:
        hours = report["offset"].total_seconds() / 3600.0
        foot += f" (GMT{hours:+g})"
    out.append(f'<div style="margin-top:30px;font-size:12px;color:#000000;">{e(foot)}.</div>')
    out.append("</div></body></html>")
    return "".join(out)


def render_text(report):
    day = report["day"]
    lines = ["Garmin In Focus",
             f"{day:%A} {day.day} {day:%B %Y} - pulled {clock(report['pulled'])} {REPORT_TZ_NAME} time", ""]
    if report.get("instead_of"):
        lines += [instead_note(report), ""]
    for t in headline_tiles(report):
        value = t["value"] + (f" {t['unit']}" if t["unit"] else "")
        lines.append(f"{t['label']}: {value}" + (f" ({t['sub']})" if t["sub"] else ""))
    for name in SECTION_ORDER:
        sec = report["sections"][name]
        lines += ["", SECTION_TITLES[name].upper()]
        lines += [f"  {label}: {value}" for label, value in sec.get("rows") or []]
        lines += [f"  {note}" for note in section_notes(name, sec)]
        if name == "sleep":
            for st in reversed(sec.get("stages") or []):
                share = f" ({round(100 * st['share'])}%)" if st.get("share") is not None else ""
                lines.append(f"  {st['name']}: {dur(st['seconds'])}{share}")
    lines += ["", "The charts are in the HTML version of this email."]
    return "\n".join(lines).replace("−", "-").replace("–", "-") + "\n"


def subject_for(report):
    return f"Garmin In Focus · {day_text(report['day'])} · {report['slot']}"


def build_message(subject, text, html, charts, address):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("Garmin In Focus", address))
    msg["To"] = address
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="vtaper.invalid")
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
        html_part = msg.get_payload()[1]
        for name, png in charts.items():
            html_part.add_related(png, maintype="image", subtype="png", cid=f"<{name}@in-focus>",
                                  filename=f"{name}.png", disposition="inline")
    return msg


def note_message(subject, paragraphs, address):
    """A short plain note, used when there is no report to send."""
    body = "".join(f'<p style="margin:0 0 12px;">{escape(p)}</p>' for p in paragraphs)
    html = (f'<!DOCTYPE html><html><body style="margin:0;padding:14px;background:#ffffff;font-family:{FONT};'
            f'color:#000000;font-size:15px;line-height:1.45;">{body}</body></html>')
    return build_message(subject, "\n\n".join(paragraphs) + "\n", html, {}, address)


# --------------------------------------------------------------------------
# Gmail
# --------------------------------------------------------------------------
def mail_settings():
    """Return (address, app password) from the environment, or stop with a
    plain message naming what is missing. Spaces in the app password are
    removed: Google shows it in four groups and it is often pasted that way."""
    address = (os.environ.get("GMAIL_ADDRESS") or "").strip()
    password = "".join((os.environ.get("GMAIL_APP_PASSWORD") or "").split())
    missing = [name for name, value in (("GMAIL_ADDRESS", address), ("GMAIL_APP_PASSWORD", password)) if not value]
    if missing:
        raise SystemExit(f"Missing GitHub secret: {' and '.join(missing)}. Add it at {SECRETS_URL}")
    if "@" not in address:
        raise SystemExit("The GMAIL_ADDRESS secret is not an email address. It should be the full Gmail address.")
    if len(password) != 16:
        log("note: GMAIL_APP_PASSWORD does not look like a Gmail app password (those are 16 letters)")
    return address, password


def gmail_session(address, password):
    try:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=40, context=ssl.create_default_context())
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"Could not reach Gmail ({type(e).__name__}). This is usually temporary.") from None
    try:
        server.login(address, password)
    except smtplib.SMTPAuthenticationError:
        server.close()
        raise SystemExit(
            "Gmail did not accept the sign-in. Check the GMAIL_ADDRESS and GMAIL_APP_PASSWORD secrets "
            f"at {SECRETS_URL}. The password must be a Gmail app password "
            "(https://myaccount.google.com/apppasswords), not the normal Google password."
        ) from None
    except Exception as e:  # noqa: BLE001
        server.close()
        raise SystemExit(f"Gmail sign-in failed ({type(e).__name__}).") from None
    return server


def check_mail():
    address, password = mail_settings()
    gmail_session(address, password).quit()
    log("Gmail sign-in works")


def send(msg, address, password):
    server = gmail_session(address, password)
    try:
        server.send_message(msg)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"Gmail accepted the sign-in but refused the email ({type(e).__name__}).") from None
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass
    # Lets the workflow know an email (report or note) really went out, so a
    # later run for the same send time does not send a second one.
    marker = os.environ.get("IN_FOCUS_SENT_FILE")
    if marker:
        try:
            Path(marker).write_text("sent\n", encoding="utf-8")
        except OSError:
            pass


# --------------------------------------------------------------------------
# Garmin
# --------------------------------------------------------------------------
def pull_garmin(g, day):
    d = day.isoformat()
    month_back = (day - timedelta(days=30)).isoformat()
    calls = {
        "stats": lambda: g.get_stats(d),
        "heart_rates": lambda: g.get_heart_rates(d),
        "body_battery": lambda: g.get_body_battery(d, d),
        "hrv": lambda: g.get_hrv_data(d),
        "sleep": lambda: g.get_sleep_data(d),
        "spo2": lambda: g.get_spo2_data(d),
        "respiration": lambda: g.get_respiration_data(d),
        "stress": lambda: g.get_stress_data(d),
        "steps": lambda: g.get_steps_data(d),
        "weight": lambda: g.get_body_composition(month_back, d),
    }
    raw = {}
    for name in CALLS:
        try:
            raw[name] = calls[name]()
        except Exception as e:  # noqa: BLE001 - keep going, that section shows "no data"
            log(f"request {name}: failed ({type(e).__name__})")
            raw[name] = None
    return raw


RENEW_NOTE = [
    "The Garmin In Focus email could not be made this time: Garmin did not accept the saved sign-in.",
    "If the next email arrives as normal, it was a passing problem at Garmin and there is nothing to do.",
    "If this note comes again, the sign-in needs renewing. On your own computer run "
    "garmin-sync/garmin_login.py (step 3 of the README in the vtaper repository), then paste the value it "
    f"copies into the GitHub secret GARMIN_TOKENS at {SECRETS_URL} . Changing the Garmin password also ends "
    "the saved sign-in.",
]
EMPTY_NOTE = [
    "The Garmin In Focus email could not be made this time: the sign-in to Garmin worked, but Garmin returned "
    "no data for any section.",
    "This is usually temporary. If the next emails are empty too, check that the watch has synced with the "
    "Garmin Connect app on the phone.",
]


# --------------------------------------------------------------------------
# Running it
# --------------------------------------------------------------------------
def parse_now(text):
    t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def write_preview(folder, report, charts, msg, html_builder):
    folder.mkdir(parents=True, exist_ok=True)
    for name, png in charts.items():
        (folder / f"{name}.png").write_bytes(png)
    (folder / "email.html").write_text(html_builder(lambda name: f"{name}.png"), encoding="utf-8")
    (folder / "email.txt").write_text(render_text(report), encoding="utf-8")
    (folder / "email.eml").write_bytes(bytes(msg))
    (folder / "subject.txt").write_text(msg["Subject"] + "\n", encoding="utf-8")


def run(args):
    if args.check_mail:
        check_mail()
        return 0

    preview = Path(args.out_dir) if args.out_dir else None
    if preview:
        address, password = "you@example.com", None
    else:
        address, password = mail_settings()  # stop early if a mail secret is missing

    now_utc = parse_now(args.now) if args.now else datetime.now(timezone.utc)
    day = date.fromisoformat(args.date) if args.date else None

    if args.from_file:
        data = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
        raw = data.get("responses", data) if isinstance(data, dict) else {}
        if not args.now and isinstance(data, dict) and data.get("now"):
            now_utc = parse_now(data["now"])
        if day is None and isinstance(data, dict) and data.get("date"):
            day = date.fromisoformat(data["date"])
        day = day or report_day(now_utc)
        report = build_report(raw, day, now_utc)
    else:
        import garmin_session

        if os.environ.get("GARMIN_SESSION_FILE"):
            # The workflow keeps the saved session in the checkout of main,
            # which is not always where this script was checked out.
            garmin_session.SESSION_FILE = Path(os.environ["GARMIN_SESSION_FILE"]).resolve()
        day = day or report_day(now_utc)
        try:
            g, seed = garmin_session.login()
        except SystemExit:
            log("Garmin sign-in failed; sending a note about it")
            msg = note_message("Garmin In Focus · Garmin sign-in did not work", RENEW_NOTE, address)
            if preview:
                preview.mkdir(parents=True, exist_ok=True)
                (preview / "email.eml").write_bytes(bytes(msg))
            else:
                send(msg, address, password)
            return 1

        def save_session():
            try:
                garmin_session.save(g, seed)
            except Exception as e:  # noqa: BLE001
                log(f"could not save the Garmin session ({type(e).__name__})")

        # Garmin can issue a new refresh token at sign-in and during the pull.
        # Save it each time straight away, before anything that could still
        # fail, so it is never lost.
        save_session()
        raw = pull_garmin(g, day)
        save_session()
        report = build_report(raw, day, now_utc)
        if not args.date and not has_day_data(report):
            # The watch may still be on the previous date (travelling west of
            # Dubai), or it has not synced since midnight.
            log("nothing recorded for this date yet; trying the day before")
            earlier = day - timedelta(days=1)
            raw = pull_garmin(g, earlier)
            save_session()
            before = build_report(raw, earlier, now_utc)
            if has_day_data(before):
                log("showing the day before")
                before["instead_of"] = day
                report, day = before, earlier

    for name in SECTION_ORDER:
        log(f"{name}: {'data' if report['sections'][name].get('has_data') else 'no data'}")

    if not has_day_data(report):
        log("no section had data; sending a short note instead of an empty report")
        msg = note_message(f"Garmin In Focus · {day_text(day)} · no data from Garmin", EMPTY_NOTE, address)
        if preview:
            preview.mkdir(parents=True, exist_ok=True)
            (preview / "email.eml").write_bytes(bytes(msg))
            (preview / "subject.txt").write_text(msg["Subject"] + "\n", encoding="utf-8")
        else:
            send(msg, address, password)
        return 1

    charts = make_charts(report)
    missing = [name for name in ALT if name not in charts]
    log(f"charts drawn: {len(charts)} of {len(ALT)}" + (f" (not drawn: {', '.join(missing)})" if missing else ""))
    lines = [p["key"] for p in report["sections"]["sleep"].get("panels") or []]
    log("lines through the night: " + (", ".join(lines) if lines else "none"))
    html = render_html(report, charts, lambda name: f"cid:{name}@in-focus")
    msg = build_message(subject_for(report), render_text(report), html, charts, address)
    if preview:
        write_preview(preview, report, charts, msg, lambda src: render_html(report, charts, src))
        log("preview written (nothing was sent)")
    else:
        send(msg, address, password)
        log("email sent")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Garmin In Focus email")
    parser.add_argument("--check-mail", action="store_true", help="only check that Gmail accepts the sign-in")
    parser.add_argument("--from-file", help="read the Garmin responses from this JSON file instead of Garmin")
    parser.add_argument("--out-dir", help="write the email and charts to this folder instead of sending")
    parser.add_argument("--date", help="report date YYYY-MM-DD (default: today in Dubai)")
    parser.add_argument("--now", help="pretend the report is made at this time (ISO, GMT unless an offset is given)")
    args = parser.parse_args(argv)
    # The Garmin library logs its own warnings, and those include the
    # account's display name. Keep everything but this script's own lines out
    # of the (public) log.
    logging.disable(logging.CRITICAL)
    warnings.simplefilter("ignore")
    try:
        return run(args)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        # Say where it went wrong, but not the error text: it could quote a value.
        frames = traceback.extract_tb(e.__traceback__)
        where = f"{Path(frames[-1].filename).name}:{frames[-1].lineno} in {frames[-1].name}" if frames else "unknown"
        log(f"stopped by an unexpected error: {type(e).__name__} at {where}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
