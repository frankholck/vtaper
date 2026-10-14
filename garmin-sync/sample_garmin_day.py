#!/usr/bin/env python3
"""
Made-up Garmin data for testing in_focus_report.py without touching Garmin.

Every number in here is invented. The shapes follow what Garmin Connect
normally returns for each call, so the report can be built, looked at and
tested on any computer:

    python sample_garmin_day.py sample.json
    python in_focus_report.py --from-file sample.json --out-dir preview

The file holds one made-up day: {"date", "now", "responses": {...}} where
"responses" has one entry per Garmin call the report makes.
"""
import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone

OFFSET = timedelta(hours=4)  # the made-up watch is on Dubai time


def _ms(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.0")


def build(day="2026-10-10", now_local="14:00", seed=7):
    """Return {"date", "now", "responses"} for one made-up day.

    day        the calendar date, YYYY-MM-DD
    now_local  the watch's local time the data runs up to, HH:MM
    """
    rnd = random.Random(seed)
    midnight_local = datetime.strptime(day, "%Y-%m-%d")
    hh, mm = (int(x) for x in now_local.split(":"))
    now = midnight_local + timedelta(hours=hh, minutes=mm)  # local, naive
    gmt = lambda local: local - OFFSET  # noqa: E731

    day_bounds = {
        "calendarDate": day,
        "startTimestampGMT": _iso(gmt(midnight_local)),
        "endTimestampGMT": _iso(gmt(midnight_local + timedelta(days=1))),
        "startTimestampLocal": _iso(midnight_local),
        "endTimestampLocal": _iso(midnight_local + timedelta(days=1)),
    }

    # ---- sleep: 23:12 the evening before until 06:48 --------------------
    sleep_start = midnight_local - timedelta(minutes=48)
    sleep_end = midnight_local + timedelta(hours=6, minutes=48)
    pattern = [  # (stage, minutes): 0 deep, 1 light, 2 REM, 3 awake
        (1, 14), (0, 38), (1, 22), (2, 16), (1, 30), (0, 34), (1, 26), (3, 4),
        (1, 18), (2, 28), (1, 36), (0, 20), (1, 24), (2, 34), (3, 6), (1, 40),
        (2, 30), (1, 36),
    ]  # adds up to the 7 h 36 min between 23:12 and 06:48
    levels, t = [], sleep_start
    seconds = {0: 0, 1: 0, 2: 0, 3: 0}
    for stage, minutes in pattern:
        end = min(t + timedelta(minutes=minutes), sleep_end)
        if end <= t:
            break
        levels.append({"startGMT": _iso(gmt(t)), "endGMT": _iso(gmt(end)), "activityLevel": float(stage)})
        seconds[stage] += int((end - t).total_seconds())
        t = end
    asleep = seconds[0] + seconds[1] + seconds[2]

    def stage_at(when):
        for seg in levels:
            a = datetime.strptime(seg["startGMT"][:19], "%Y-%m-%dT%H:%M:%S") + OFFSET
            b = datetime.strptime(seg["endGMT"][:19], "%Y-%m-%dT%H:%M:%S") + OFFSET
            if a <= when < b:
                return int(seg["activityLevel"])
        return None

    sleep_hr, hrv_data, sleep_stress, sleep_bb, spo2_epochs, resp_epochs, hrv_readings = [], [], [], [], [], [], []
    minutes_total = int((sleep_end - sleep_start).total_seconds() // 60)
    for i in range(0, minutes_total + 1):
        when = sleep_start + timedelta(minutes=i)
        frac = i / minutes_total
        stage = stage_at(when)
        base_hr = 56 - 8 * math.sin(math.pi * min(frac * 1.15, 1.0)) + (4 if stage == 2 else 0) + (7 if stage == 3 else 0)
        if i % 2 == 0:
            sleep_hr.append({"value": round(base_hr + rnd.uniform(-1.5, 1.5)), "startGMT": _ms(gmt(when))})
            resp_epochs.append({"startTimeGMT": _ms(gmt(when)), "respirationValue": round(13.5 + (1.2 if stage == 2 else 0) + rnd.uniform(-0.8, 0.8), 1)})
        if i % 5 == 0:
            hrv = round(46 + 9 * math.sin(math.pi * frac) - (6 if stage == 3 else 0) + rnd.uniform(-5, 5))
            hrv_data.append({"value": float(hrv), "startGMT": _ms(gmt(when))})
            hrv_readings.append({"hrvValue": hrv, "readingTimeGMT": _iso(gmt(when)), "readingTimeLocal": _iso(when)})
        if i % 3 == 0:
            sleep_stress.append({"value": max(1, round(14 - 6 * math.sin(math.pi * frac) + rnd.uniform(-4, 6))), "startGMT": _ms(gmt(when))})
            sleep_bb.append({"value": round(31 + 61 * frac), "startGMT": _ms(gmt(when))})
        dip = -3 if 205 < i < 214 else 0
        spo2_epochs.append({
            "epochTimestamp": _iso(gmt(when)),
            "calendarDate": day,
            "epochDuration": 60,
            "spo2Reading": max(88, min(99, round(95.5 + dip + rnd.uniform(-1.4, 1.4)))),
            "readingConfidence": 2,
        })

    sleep = {
        "dailySleepDTO": {
            "calendarDate": day,
            "sleepTimeSeconds": asleep,
            "napTimeSeconds": 0,
            "sleepStartTimestampGMT": _ms(gmt(sleep_start)),
            "sleepEndTimestampGMT": _ms(gmt(sleep_end)),
            "sleepStartTimestampLocal": _ms(sleep_start),
            "sleepEndTimestampLocal": _ms(sleep_end),
            "deepSleepSeconds": seconds[0],
            "lightSleepSeconds": seconds[1],
            "remSleepSeconds": seconds[2],
            "awakeSleepSeconds": seconds[3],
            "awakeCount": 2,
            "avgSleepStress": 13.0,
            "averageSpO2Value": 95.0,
            "lowestSpO2Value": 91,
            "highestSpO2Value": 99,
            "averageRespirationValue": 14.0,
            "lowestRespirationValue": 12.0,
            "highestRespirationValue": 17.0,
            "avgHeartRate": 51.0,
            "sleepScores": {
                "overall": {"value": 84, "qualifierKey": "GOOD"},
                "remPercentage": {"value": 24, "qualifierKey": "EXCELLENT"},
                "deepPercentage": {"value": 20, "qualifierKey": "GOOD"},
                "lightPercentage": {"value": 54, "qualifierKey": "GOOD"},
            },
        },
        "sleepLevels": levels,
        "sleepHeartRate": sleep_hr,
        "hrvData": hrv_data,
        "sleepStress": sleep_stress,
        "sleepBodyBattery": sleep_bb,
        "wellnessEpochSPO2DataDTOList": spo2_epochs,
        "wellnessEpochRespirationDataDTOList": resp_epochs,
        "avgOvernightHrv": 49.0,
        "hrvStatus": "BALANCED",
        "bodyBatteryChange": 61,
        "restingHeartRate": 48,
    }

    # ---- through the day -------------------------------------------------
    def awake_hr(when):
        h = when.hour + when.minute / 60
        if 12.0 <= h < 13.0:  # a made-up gym session
            return 118 + 26 * math.sin(math.pi * (h - 12.0)) + rnd.uniform(-9, 9)
        if 7.0 <= h < 7.6:  # morning walk
            return 92 + rnd.uniform(-6, 8)
        return 66 + 6 * math.sin(h / 2.1) + rnd.uniform(-4, 6)

    heart_values, stress_values, bb_rows, bb_rows4 = [], [], [], []
    battery = 31.0 + 61.0 * (48 / minutes_total)  # level at midnight
    when = midnight_local
    while when <= now:
        asleep_now = when < sleep_end
        if asleep_now:
            i = int((when - sleep_start).total_seconds() // 60)
            frac = i / minutes_total
            hr = 56 - 8 * math.sin(math.pi * min(frac * 1.15, 1.0)) + rnd.uniform(-1.5, 1.5)
        else:
            hr = awake_hr(when)
        off_wrist = timedelta(hours=9, minutes=40) <= when - midnight_local < timedelta(hours=10, minutes=10)
        heart_values.append([_ms(gmt(when)), None if off_wrist else round(hr)])
        when += timedelta(minutes=2)

    when = midnight_local
    while when <= now:
        h = when.hour + when.minute / 60
        asleep_now = when < sleep_end
        off_wrist = 9 + 40 / 60 <= h < 10 + 10 / 60
        if asleep_now:
            level = max(1, round(13 + rnd.uniform(-5, 6)))
            battery = min(92.0, battery + 61.0 / (minutes_total / 3))
        elif 12.0 <= h < 13.0:
            level = -2  # too active to measure
            battery -= 0.75
        elif off_wrist:
            level = -1  # not measured
        else:
            level = max(5, min(96, round(34 + 16 * math.sin(h * 1.7) + rnd.uniform(-12, 18))))
            battery -= 0.11 + level / 900
        stress_values.append([_ms(gmt(when)), level])
        if not off_wrist:
            bb_rows.append([_ms(gmt(when)), round(battery)])
            bb_rows4.append([_ms(gmt(when)), "MEASURED", round(battery), 2.0])
        when += timedelta(minutes=3)

    steps_rows, total_steps = [], 0
    when = midnight_local
    while when < now:
        h = when.hour + when.minute / 60
        if when < sleep_end:
            steps = 0
        elif 7.0 <= h < 7.6:
            steps = rnd.randint(650, 980)
        elif 12.0 <= h < 13.0:
            steps = rnd.randint(40, 260)
        else:
            steps = rnd.choice([0, 0, 12, 35, 60, 110, 180, 240, 420])
        total_steps += steps
        steps_rows.append({
            "startGMT": _iso(gmt(when)),
            "endGMT": _iso(gmt(when + timedelta(minutes=15))),
            "steps": steps,
            "pushes": 0,
            "primaryActivityLevel": "sedentary" if steps < 50 else "active",
            "activityLevelConstant": True,
        })
        when += timedelta(minutes=15)

    measured_hr = [v for _, v in heart_values if v is not None]
    measured_stress = [v for _, v in stress_values if v >= 0]
    measured_bb = [row[1] for row in bb_rows]
    counts = {"rest": 0, "low": 0, "medium": 0, "high": 0}
    for v in measured_stress:
        counts["rest" if v <= 25 else "low" if v <= 50 else "medium" if v <= 75 else "high"] += 1

    stats = {
        "calendarDate": day,
        "totalSteps": total_steps,
        "dailyStepGoal": 10000,
        "totalDistanceMeters": round(total_steps * 0.78),
        "restingHeartRate": 48,
        "minHeartRate": min(measured_hr),
        "maxHeartRate": max(measured_hr),
        "lastSevenDaysAvgRestingHeartRate": 49,
        "averageStressLevel": round(sum(measured_stress) / len(measured_stress)),
        "maxStressLevel": max(measured_stress),
        "restStressDuration": counts["rest"] * 180,
        "lowStressDuration": counts["low"] * 180,
        "mediumStressDuration": counts["medium"] * 180,
        "highStressDuration": counts["high"] * 180,
        "bodyBatteryChargedValue": 61,
        "bodyBatteryDrainedValue": max(0, max(measured_bb) - measured_bb[-1]),
        "bodyBatteryHighestValue": max(measured_bb),
        "bodyBatteryLowestValue": min(measured_bb),
        "bodyBatteryMostRecentValue": measured_bb[-1],
        "bodyBatteryAtWakeTime": 92,
        "averageSpo2": 95.0,
        "lowestSpo2": 91,
        "latestSpo2": 96,
    }

    heart_rates = dict(day_bounds)
    heart_rates.update({
        "maxHeartRate": stats["maxHeartRate"],
        "minHeartRate": stats["minHeartRate"],
        "restingHeartRate": 48,
        "lastSevenDaysAvgRestingHeartRate": 49,
        "heartRateValueDescriptors": [{"key": "timestamp", "index": 0}, {"key": "heartrate", "index": 1}],
        "heartRateValues": heart_values,
    })

    stress = dict(day_bounds)
    stress.update({
        "maxStressLevel": stats["maxStressLevel"],
        "avgStressLevel": stats["averageStressLevel"],
        "stressValueDescriptorsDTOList": [{"key": "timestamp", "index": 0}, {"key": "stressLevel", "index": 1}],
        "stressValuesArray": stress_values,
        "bodyBatteryValueDescriptorsDTOList": [
            {"key": "timestamp", "index": 0},
            {"key": "bodyBatteryStatus", "index": 1},
            {"key": "bodyBatteryLevel", "index": 2},
            {"key": "bodyBatteryVersion", "index": 3},
        ],
        "bodyBatteryValuesArray": bb_rows4,
    })

    body_battery = [{
        "date": day,
        "charged": 61,
        "drained": stats["bodyBatteryDrainedValue"],
        "startTimestampGMT": day_bounds["startTimestampGMT"],
        "endTimestampGMT": day_bounds["endTimestampGMT"],
        "startTimestampLocal": day_bounds["startTimestampLocal"],
        "endTimestampLocal": day_bounds["endTimestampLocal"],
        "bodyBatteryValuesArray": bb_rows,
        "bodyBatteryValueDescriptorDTOList": [
            {"bodyBatteryValueDescriptorIndex": 0, "bodyBatteryValueDescriptorKey": "timestamp"},
            {"bodyBatteryValueDescriptorIndex": 1, "bodyBatteryValueDescriptorKey": "bodyBatteryLevel"},
        ],
    }]

    hrv = dict(day_bounds)
    hrv.update({
        "hrvSummary": {
            "calendarDate": day,
            "weeklyAvg": 47,
            "lastNightAvg": 49,
            "lastNight5MinHigh": 68,
            "baseline": {"lowUpper": 38, "balancedLow": 42, "balancedUpper": 56, "markerValue": 0.46},
            "status": "BALANCED",
        },
        "hrvReadings": hrv_readings,
    })

    spo2 = dict(day_bounds)
    spo2.update({
        "averageSpO2": 95.0,
        "lowestSpO2": 91,
        "lastSevenDaysAvgSpO2": 95.2,
        "latestSpO2": 96,
        "avgSleepSpO2": 95.0,
        "spO2HourlyAverages": [[_ms(gmt(midnight_local + timedelta(hours=h))), 95] for h in range(0, 7)],
    })

    respiration = dict(day_bounds)
    respiration.update({
        "lowestRespirationValue": 12.0,
        "highestRespirationValue": 19.0,
        "avgWakingRespirationValue": 15.0,
        "avgSleepRespirationValue": 14.0,
        "respirationValueDescriptorsDTOList": [{"key": "timestamp", "index": 0}, {"key": "respiration", "index": 1}],
        "respirationValuesArray": [[e["startTimeGMT"], e["respirationValue"]] for e in resp_epochs],
    })

    weigh_time = midnight_local + timedelta(hours=7, minutes=5)
    earlier = midnight_local - timedelta(days=3) + timedelta(hours=7, minutes=20)
    weight = {
        "startDate": (midnight_local - timedelta(days=30)).strftime("%Y-%m-%d"),
        "endDate": day,
        "dateWeightList": [
            {"calendarDate": earlier.strftime("%Y-%m-%d"), "date": _ms(earlier), "timestampGMT": _ms(gmt(earlier)),
             "weight": 86400.0, "bodyFat": 17.9, "sourceType": "INDEX_SCALE"},
            {"calendarDate": day, "date": _ms(weigh_time), "timestampGMT": _ms(gmt(weigh_time)),
             "weight": 85900.0, "bodyFat": 17.6, "sourceType": "INDEX_SCALE"},
        ],
    }

    return {
        "_note": "Made-up data for testing. Not from a real Garmin account.",
        "date": day,
        "now": _iso(gmt(now)) + "Z",
        "responses": {
            "stats": stats,
            "heart_rates": heart_rates,
            "body_battery": body_battery,
            "hrv": hrv,
            "sleep": sleep,
            "spo2": spo2,
            "respiration": respiration,
            "stress": stress,
            "steps": steps_rows,
            "weight": weight,
        },
    }


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "sample.json"
    now_local = sys.argv[2] if len(sys.argv) > 2 else "14:00"
    with open(out, "w") as f:
        json.dump(build(now_local=now_local), f)
    print(f"Wrote made-up Garmin data to {out}")
