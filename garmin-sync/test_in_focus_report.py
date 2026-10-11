#!/usr/bin/env python3
"""
Tests for in_focus_report.py. They use made-up data only (sample_garmin_day.py),
never Garmin or Gmail:

    python -m unittest test_in_focus_report -v      (run inside garmin-sync/)
"""
import contextlib
import copy
import io
import json
import os
import re
import smtplib
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import in_focus_report as r  # noqa: E402
import sample_garmin_day  # noqa: E402

DAY = date(2026, 10, 10)
ALLOWED_LOG = re.compile(
    r"^\[in-focus\] ("
    r"(heart rate|Body Battery|HRV|sleep|stress|steps|weight): (data|no data)"
    r"|charts drawn: \d of 6( \(not drawn: [a-z_, ]+\))?"
    r"|nothing recorded for this date yet; trying the day before"
    r"|showing the day before"
    r"|lines through the night: [A-Za-z, ]+"
    r"|preview written \(nothing was sent\)"
    r"|email sent"
    r"|Gmail sign-in works"
    r"|no section had data; sending a short note instead of an empty report"
    r"|Garmin sign-in failed; sending a note about it"
    r"|request [a-z0-9_]+: failed \([A-Za-z]+\)"
    r")$"
)


def sample(now_local="14:00"):
    return sample_garmin_day.build(day=DAY.isoformat(), now_local=now_local)


def report_for(data):
    return r.build_report(data["responses"], DAY, r.parse_now(data["now"]))


class FakeSMTP:
    """Stands in for smtplib.SMTP_SSL so no test ever talks to Gmail."""
    instances = []
    reject_login = False
    reject_send = False

    def __init__(self, host, port, **kwargs):
        self.host, self.port, self.logins, self.sent = host, port, [], []
        FakeSMTP.instances.append(self)

    def login(self, user, password):
        self.logins.append((user, password))
        if FakeSMTP.reject_login:
            raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")

    def send_message(self, msg):
        if FakeSMTP.reject_send:
            raise smtplib.SMTPDataError(552, b"refused")
        self.sent.append(msg)

    def quit(self):
        pass

    def close(self):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        FakeSMTP.instances, FakeSMTP.reject_login, FakeSMTP.reject_send = [], False, False
        patches = [
            mock.patch.object(smtplib, "SMTP_SSL", FakeSMTP),
            mock.patch.dict(os.environ, {"GMAIL_ADDRESS": "someone@example.com",
                                         "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("GARMIN_SESSION_FILE", None)

    def run_main(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                code = r.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, err.getvalue()


class ReadingTests(unittest.TestCase):
    def test_times_in_every_form_garmin_uses(self):
        expected = datetime(2026, 10, 9, 19, 12)
        ms = int(expected.replace(tzinfo=timezone.utc).timestamp() * 1000)
        for value in (ms, float(ms), str(ms), ms // 1000, "2026-10-09T19:12:00.0", "2026-10-09T19:12:00",
                      "2026-10-09 19:12", "2026-10-09T19:12:00.000Z"):
            self.assertEqual(r.to_utc(value), expected, value)
        for junk in (None, True, "", "soon", "2026-13-45T99:99:99", [], {}):
            self.assertIsNone(r.to_utc(junk), junk)

    def test_numbers(self):
        self.assertEqual(r.num("57"), 57.0)
        self.assertEqual(r.num(0), 0.0)
        for junk in (None, True, "n/a", float("nan"), float("inf"), [], {}):
            self.assertIsNone(r.num(junk), junk)
        self.assertEqual(r.pick(None, "x", 0, 5), 0.0)

    def test_column_names_from_either_descriptor_style(self):
        a = r.descriptor_columns([{"key": "timestamp", "index": 0}, {"key": "bodyBatteryLevel", "index": 2}])
        b = r.descriptor_columns([{"bodyBatteryValueDescriptorIndex": 1, "bodyBatteryValueDescriptorKey": "bodyBatteryLevel"}])
        self.assertEqual(a, {"timestamp": 0, "bodybatterylevel": 2})
        self.assertEqual(b, {"bodybatterylevel": 1})
        self.assertEqual(r.descriptor_columns("junk"), {})

    def test_durations(self):
        self.assertEqual(r.dur(7 * 3600 + 6 * 60), "7 h 6 min")
        self.assertEqual(r.dur(3600), "1 h")
        self.assertEqual(r.dur(600), "10 min")
        self.assertEqual(r.dur_short(5520), "1h 32m")


class ReportTests(Base):
    def test_full_day_has_every_section_and_chart(self):
        report = report_for(sample())
        for name in r.SECTION_ORDER:
            self.assertTrue(report["sections"][name]["has_data"], name)
        charts = r.make_charts(report)
        self.assertEqual(sorted(charts), sorted(r.ALT))
        for png in charts.values():
            self.assertTrue(png.startswith(b"\x89PNG"))
        html = r.render_html(report, charts, lambda n: f"cid:{n}@in-focus")
        for title in r.SECTION_TITLES.values():
            self.assertIn(f">{title}</div>", html)
        for name in charts:
            self.assertIn(f"cid:{name}@in-focus", html)
        self.assertNotIn("No data from Garmin", html)

    def test_numbers_match_the_made_up_data(self):
        data = sample()
        s = report_for(data)["sections"]
        stats = data["responses"]["stats"]
        self.assertEqual(s["heart rate"]["resting"], 48)
        self.assertEqual(s["Body Battery"]["current"], stats["bodyBatteryMostRecentValue"])
        self.assertEqual(s["HRV"]["last"], 49)
        self.assertEqual(s["HRV"]["band"], (42, 56))
        self.assertEqual(s["sleep"]["score"], 84)
        self.assertEqual(s["steps"]["total"], stats["totalSteps"])
        self.assertAlmostEqual(s["weight"]["kg"], 85.9)
        self.assertTrue(s["weight"]["today"])
        # stage seconds add up to the time in bed, 23:12 to 06:48
        self.assertEqual(sum(st["seconds"] for st in s["sleep"]["stages"]), (7 * 60 + 36) * 60)
        self.assertEqual(r.clock(s["sleep"]["start"]), "23:12")
        self.assertEqual(r.clock(s["sleep"]["end"]), "06:48")
        # hourly steps add up to the 15-minute rows
        self.assertEqual(sum(s["steps"]["hourly"]), sum(row["steps"] for row in data["responses"]["steps"]))
        # readings not measured (off the wrist, or stress below zero) are left out
        self.assertTrue(all(0 <= v <= 100 for _, v in s["stress"]["series"]))
        self.assertEqual(len(s["heart rate"]["series"]),
                         sum(1 for _, v in data["responses"]["heart_rates"]["heartRateValues"] if v is not None))

    def test_three_send_times(self):
        for now_local, hours in (("07:30", 12), ("14:00", 18), ("22:30", 24)):
            report = report_for(sample(now_local))
            self.assertEqual(report["ctx"]["axis_hours"], hours)
            self.assertEqual(report["slot"], now_local)
            self.assertEqual(r.subject_for(report), f"Garmin In Focus · Sat 10 Oct · {now_local}")
            self.assertEqual(len(r.make_charts(report)), 6)

    def test_a_late_run_keeps_its_slot_but_an_odd_time_shows_the_clock(self):
        data = sample("14:00")
        late = r.build_report(data["responses"], DAY, r.parse_now("2026-10-10T10:25:00Z"))  # 14:25 Dubai
        self.assertEqual(late["slot"], "14:00")
        manual = r.build_report(data["responses"], DAY, r.parse_now("2026-10-10T12:10:00Z"))  # 16:10 Dubai
        self.assertEqual(manual["slot"], "16:10")

    def test_sleep_not_synced_says_so(self):
        for broken in ({}, None, {"dailySleepDTO": {"calendarDate": DAY.isoformat(), "sleepTimeSeconds": None}}):
            data = sample("07:30")
            data["responses"]["sleep"] = broken
            data["responses"]["hrv"] = None
            report = report_for(data)
            self.assertFalse(report["sections"]["sleep"]["has_data"])
            charts = r.make_charts(report)
            self.assertNotIn("sleep_stages", charts)
            self.assertNotIn("overnight", charts)
            html = r.render_html(report, charts, lambda n: n)
            self.assertIn("Last night&#x27;s sleep has not synced from the watch yet.", html)
            self.assertIn("Last night&#x27;s HRV has not synced from the watch yet.", html)
            self.assertTrue(report["sections"]["heart rate"]["has_data"])

    def test_dates_with_a_time_attached_still_count_as_the_same_day(self):
        data = sample()
        data["responses"]["sleep"]["dailySleepDTO"]["calendarDate"] = "2026-10-10T00:00:00.0"
        data["responses"]["hrv"]["hrvSummary"]["calendarDate"] = "2026-10-10T00:00:00.0"
        s = report_for(data)["sections"]
        self.assertTrue(s["sleep"]["has_data"])
        self.assertTrue(s["HRV"]["has_data"])

    def test_the_previous_night_is_never_shown_as_last_night(self):
        data = sample("07:30")
        data["responses"]["sleep"]["dailySleepDTO"]["calendarDate"] = "2026-10-09"
        data["responses"]["hrv"]["hrvSummary"]["calendarDate"] = "2026-10-09"
        s = report_for(data)["sections"]
        self.assertFalse(s["sleep"]["has_data"])
        self.assertFalse(s["HRV"]["has_data"])

    def test_no_weigh_in_today_shows_the_last_one_with_its_date(self):
        data = sample()
        data["responses"]["weight"]["dateWeightList"].pop()  # leaves the one from 7 Oct
        w = report_for(data)["sections"]["weight"]
        self.assertTrue(w["has_data"])
        self.assertFalse(w["today"])
        self.assertEqual(w["rows"][0], ("Last weigh-in (Wed 7 Oct)", "86.4 kg"))
        self.assertIn("No weigh-in today", w["note"])
        data["responses"]["weight"]["dateWeightList"] = []
        self.assertFalse(report_for(data)["sections"]["weight"]["has_data"])

    def test_weigh_ins_are_read_newest_last_whatever_order_garmin_sends(self):
        data = sample()
        data["responses"]["weight"]["dateWeightList"].reverse()
        w = report_for(data)["sections"]["weight"]
        self.assertAlmostEqual(w["kg"], 85.9)
        self.assertEqual(w["rows"][1], ("Change since Wed 7 Oct", "−0.5 kg"))

    def test_overnight_lines_fall_back_to_the_day_responses(self):
        data = sample()
        for key in ("sleepHeartRate", "hrvData", "wellnessEpochSPO2DataDTOList", "wellnessEpochRespirationDataDTOList"):
            data["responses"]["sleep"].pop(key)
        panels = {p["key"] for p in report_for(data)["sections"]["sleep"]["panels"]}
        self.assertEqual(panels, {"heart rate", "HRV", "Pulse Ox", "respiration"})

    def test_body_battery_falls_back_to_the_stress_response(self):
        data = sample()
        data["responses"]["body_battery"] = []
        bb = report_for(data)["sections"]["Body Battery"]
        self.assertTrue(len(bb["series"]) > 100)
        self.assertTrue(all(0 <= v <= 100 for _, v in bb["series"]))

    def test_watch_on_another_time_zone(self):
        data = sample()
        hr = data["responses"]["heart_rates"]
        hr["startTimestampLocal"] = "2026-10-10T04:00:00.0"  # GMT+8 instead of Dubai's +4
        report = report_for(data)
        self.assertEqual(report["offset"], timedelta(hours=8))
        self.assertTrue(report["watch_differs"])
        self.assertIn("(GMT+8)", r.render_html(report, {}, lambda n: n))
        self.assertFalse(report_for(sample())["watch_differs"])

    def test_empty_and_junk_responses_never_crash(self):
        junk_sets = [
            {},
            {name: None for name in r.CALLS},
            {name: {} for name in r.CALLS},
            {name: [] for name in r.CALLS},
            {name: "unexpected" for name in r.CALLS},
            {name: 7 for name in r.CALLS},
            {
                "stats": {"totalSteps": "many", "restingHeartRate": None, "averageStressLevel": -1},
                "heart_rates": {"heartRateValues": [[None, None], "x", [1], [1760000000000, "fast"]],
                                "heartRateValueDescriptors": [{"key": "heartrate", "index": 9}]},
                "body_battery": [None, "x", {"bodyBatteryValuesArray": [[1760000000000]]}],
                "hrv": {"hrvSummary": "none", "hrvReadings": [{"hrvValue": None}, 3]},
                "sleep": {"dailySleepDTO": {"sleepTimeSeconds": 25000, "sleepStartTimestampGMT": "bad",
                                            "sleepScores": {"overall": "good"}},
                          "sleepLevels": [{"startGMT": "x"}, 5, {"startGMT": "2026-10-09T20:00:00.0",
                                                                 "endGMT": "2026-10-09T19:00:00.0", "activityLevel": 9}]},
                "spo2": {"spO2SingleValues": [["a", "b"]]},
                "respiration": {"respirationValuesArray": {"not": "a list"}},
                "stress": {"stressValuesArray": [[1760000000000, -2], [1760000180000, None]]},
                "steps": [{"startGMT": None, "steps": 10}, "x"],
                "weight": {"dateWeightList": [{"weight": None}, {"weight": 85000, "calendarDate": "not a date"}, 4]},
            },
        ]
        for raw in junk_sets:
            report = r.build_report(raw, DAY, r.parse_now("2026-10-10T10:00:00Z"))
            charts = r.make_charts(report)
            html = r.render_html(report, charts, lambda n: n)
            self.assertIn("Garmin In Focus", html)
            self.assertIn("Garmin In Focus", r.render_text(report))

    def test_one_reading_is_not_drawn_as_a_chart(self):
        data = sample()
        data["responses"]["heart_rates"]["heartRateValues"] = data["responses"]["heart_rates"]["heartRateValues"][:1]
        charts = r.make_charts(report_for(data))
        self.assertNotIn("heart_rate", charts)


class EmailTests(Base):
    def test_message_carries_the_charts_inline(self):
        report = report_for(sample())
        charts = r.make_charts(report)
        html = r.render_html(report, charts, lambda n: f"cid:{n}@in-focus")
        msg = r.build_message(r.subject_for(report), r.render_text(report), html, charts, "someone@example.com")
        self.assertEqual(msg["To"], "someone@example.com")
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        plain, related = msg.get_payload()
        self.assertEqual(plain.get_content_type(), "text/plain")
        self.assertEqual(related.get_content_type(), "multipart/related")
        parts = related.get_payload()
        self.assertEqual(parts[0].get_content_type(), "text/html")
        cids = {p["Content-ID"] for p in parts[1:]}
        self.assertEqual(cids, {f"<{name}@in-focus>" for name in charts})
        self.assertTrue(all(p.get_content_type() == "image/png" for p in parts[1:]))
        self.assertLess(len(bytes(msg)), 2_000_000)

    def test_text_is_black_on_white_with_no_grey(self):
        report = report_for(sample())
        html = r.render_html(report, {}, lambda n: n)
        colours = set(re.findall(r"(?<![-\w])color:(#[0-9a-fA-F]{6})", html))
        self.assertEqual(colours, {"#000000"})
        self.assertIn("background:#ffffff", html)

    def test_preview_folder_and_quiet_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp) / "sample.json", Path(tmp) / "preview"
            src.write_text(json.dumps(sample()))
            code, log = self.run_main("--from-file", str(src), "--out-dir", str(out))
            self.assertEqual(code, 0)
            for name in ("email.html", "email.txt", "email.eml", "subject.txt", *(f"{n}.png" for n in r.ALT)):
                self.assertTrue((out / name).exists(), name)
            self.assertEqual(FakeSMTP.instances, [])  # a preview never signs in to Gmail
            for line in log.strip().splitlines():
                self.assertRegex(line, ALLOWED_LOG)

    def test_from_file_sends_through_gmail_with_spaces_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "sample.json"
            src.write_text(json.dumps(sample()))
            code, log = self.run_main("--from-file", str(src))
        self.assertEqual(code, 0)
        (server,) = FakeSMTP.instances
        self.assertEqual((server.host, server.port), ("smtp.gmail.com", 465))
        self.assertEqual(server.logins, [("someone@example.com", "abcdefghijklmnop")])
        self.assertEqual(len(server.sent), 1)
        self.assertEqual(server.sent[0]["Subject"], "Garmin In Focus · Sat 10 Oct · 14:00")
        for line in log.strip().splitlines():
            self.assertRegex(line, ALLOWED_LOG)
        self.assertNotIn("example.com", log)

    def test_all_empty_sends_a_short_note_and_fails_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "empty.json"
            src.write_text(json.dumps({"date": DAY.isoformat(), "now": "2026-10-10T10:00:00Z",
                                       "responses": {name: None for name in r.CALLS}}))
            code, _ = self.run_main("--from-file", str(src))
        self.assertEqual(code, 1)
        (server,) = FakeSMTP.instances
        self.assertIn("no data from Garmin", server.sent[0]["Subject"])


class GmailTests(Base):
    def test_check_mail_signs_in_without_sending(self):
        code, log = self.run_main("--check-mail")
        self.assertEqual(code, 0)
        (server,) = FakeSMTP.instances
        self.assertEqual(server.logins, [("someone@example.com", "abcdefghijklmnop")])
        self.assertEqual(server.sent, [])
        self.assertIn("Gmail sign-in works", log)

    def test_missing_secret_is_named_plainly(self):
        for missing, expect in (("GMAIL_APP_PASSWORD", "GMAIL_APP_PASSWORD"), ("GMAIL_ADDRESS", "GMAIL_ADDRESS")):
            with mock.patch.dict(os.environ, {missing: "  "}):
                code, _ = self.run_main("--check-mail")
            self.assertIn(f"Missing GitHub secret: {expect}.", str(code))
            self.assertEqual(FakeSMTP.instances, [])
        with mock.patch.dict(os.environ, {"GMAIL_ADDRESS": "", "GMAIL_APP_PASSWORD": ""}):
            code, _ = self.run_main()  # a normal run stops before Garmin is touched
        self.assertIn("GMAIL_ADDRESS and GMAIL_APP_PASSWORD", str(code))

    def test_rejected_password_is_explained_and_never_echoed(self):
        FakeSMTP.reject_login = True
        code, log = self.run_main("--check-mail")
        self.assertIn("Gmail did not accept the sign-in", str(code))
        for secret in ("abcdefghijklmnop", "abcd efgh", "someone@example.com"):
            self.assertNotIn(secret, str(code) + log)


class FakeGarmin:
    """Answers the same calls as garminconnect.Garmin, from made-up data."""

    def __init__(self, responses, fail=(), empty_dates=()):
        self.responses, self.fail, self.calls = responses, set(fail), []
        self.empty_dates = set(empty_dates)

    def _answer(self, name, *args):
        self.calls.append((name, args))
        if name in self.fail:
            raise RuntimeError("secret detail that must not reach the log: 57")
        if args[-1] in self.empty_dates:  # nothing recorded for that date
            return [] if name in ("steps", "body_battery") else {}
        return copy.deepcopy(self.responses[name])

    def get_stats(self, cdate):
        return self._answer("stats", cdate)

    def get_heart_rates(self, cdate):
        return self._answer("heart_rates", cdate)

    def get_body_battery(self, startdate, enddate=None):
        return self._answer("body_battery", startdate, enddate)

    def get_hrv_data(self, cdate):
        return self._answer("hrv", cdate)

    def get_sleep_data(self, cdate):
        return self._answer("sleep", cdate)

    def get_spo2_data(self, cdate):
        return self._answer("spo2", cdate)

    def get_respiration_data(self, cdate):
        return self._answer("respiration", cdate)

    def get_stress_data(self, cdate):
        return self._answer("stress", cdate)

    def get_steps_data(self, cdate):
        return self._answer("steps", cdate)

    def get_body_composition(self, startdate, enddate=None):
        return self._answer("weight", startdate, enddate)


class GarminRunTests(Base):
    def fake_session(self, client=None, login_fails=False):
        events = []
        module = types.ModuleType("garmin_session")
        module.SESSION_FILE = Path("unset")

        def login():
            events.append("login")
            if login_fails:
                raise SystemExit("Garmin session is missing or expired.")
            return client, "seed"

        def save(g, seed):
            events.append("save")
            return True

        module.login, module.save = login, save
        patcher = mock.patch.dict(sys.modules, {"garmin_session": module})
        patcher.start()
        self.addCleanup(patcher.stop)
        return module, events

    def test_pull_and_send(self):
        data = sample()
        client = FakeGarmin(data["responses"])
        module, events = self.fake_session(client)
        code, log = self.run_main("--now", data["now"])
        self.assertEqual(code, 0)
        self.assertEqual(events, ["login", "save", "save"])  # after sign-in, and again after the pull
        self.assertEqual([name for name, _ in client.calls], list(r.CALLS))
        self.assertEqual(dict(client.calls)["stats"], ("2026-10-10",))
        self.assertEqual(dict(client.calls)["weight"], ("2026-09-10", "2026-10-10"))
        (server,) = FakeSMTP.instances
        self.assertEqual(len(server.sent), 1)
        for line in log.strip().splitlines():
            self.assertRegex(line, ALLOWED_LOG)

    def test_session_is_saved_before_anything_that_can_still_fail(self):
        data = sample()
        module, events = self.fake_session(FakeGarmin(data["responses"]))
        FakeSMTP.reject_send = True
        code, _ = self.run_main("--now", data["now"])
        self.assertIn("refused the email", str(code))
        self.assertEqual(events, ["login", "save", "save"])

    def test_session_file_can_live_in_another_checkout(self):
        data = sample()
        module, _ = self.fake_session(FakeGarmin(data["responses"]))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "session.enc"
            with mock.patch.dict(os.environ, {"GARMIN_SESSION_FILE": str(target)}):
                code, _ = self.run_main("--now", data["now"])
            self.assertEqual(code, 0)
            self.assertEqual(module.SESSION_FILE, target.resolve())

    def test_failed_requests_leave_gaps_not_a_crash_and_stay_out_of_the_log(self):
        data = sample()
        client = FakeGarmin(data["responses"], fail=("sleep", "hrv", "weight"))
        self.fake_session(client)
        code, log = self.run_main("--now", data["now"])
        self.assertEqual(code, 0)
        self.assertIn("request sleep: failed (RuntimeError)", log)
        self.assertNotIn("secret detail", log)
        self.assertIn("sleep: no data", log)
        self.assertIn("heart rate: data", log)
        self.assertIn("charts drawn: 4 of 6 (not drawn: sleep_stages, overnight)", log)
        self.assertIn("lines through the night: none", log)
        for line in log.strip().splitlines():
            self.assertRegex(line, ALLOWED_LOG)

    def test_a_late_evening_run_still_reports_the_day_that_just_ended(self):
        data = sample("22:30")
        client = FakeGarmin(data["responses"])
        self.fake_session(client)
        code, _ = self.run_main("--now", "2026-10-10T20:40:00Z")  # 00:40 on the 11th in Dubai
        self.assertEqual(code, 0)
        self.assertEqual(dict(client.calls)["stats"], ("2026-10-10",))
        self.assertEqual(FakeSMTP.instances[0].sent[0]["Subject"], "Garmin In Focus · Sat 10 Oct · 00:40")

    def test_nothing_for_today_falls_back_to_the_day_before_and_says_so(self):
        # The watch has not reached this date yet (travelling west) or has not synced since midnight.
        data = sample_garmin_day.build(day="2026-10-09", now_local="23:56")
        client = FakeGarmin(data["responses"], empty_dates={"2026-10-10"})
        _, events = self.fake_session(client)
        code, log = self.run_main("--now", "2026-10-10T03:30:00Z")
        self.assertEqual(code, 0)
        self.assertEqual([args[-1] for _, args in client.calls], ["2026-10-10"] * 10 + ["2026-10-09"] * 10)
        self.assertEqual(events, ["login", "save", "save", "save"])
        msg = FakeSMTP.instances[0].sent[0]
        self.assertEqual(msg["Subject"], "Garmin In Focus · Fri 9 Oct · 07:30")
        html = msg.get_body(preferencelist=("html",)).get_content()
        self.assertIn("Nothing has synced for Saturday 10 October yet, so this is Friday 9 October.", html)
        self.assertIn("showing the day before", log)
        for line in log.strip().splitlines():
            self.assertRegex(line, ALLOWED_LOG)

    def test_nothing_for_either_day_sends_the_no_data_note(self):
        client = FakeGarmin(sample()["responses"], empty_dates={"2026-10-10", "2026-10-09"})
        self.fake_session(client)
        code, _ = self.run_main("--now", "2026-10-10T03:30:00Z")
        self.assertEqual(code, 1)
        self.assertIn("no data from Garmin", FakeSMTP.instances[0].sent[0]["Subject"])

    def test_the_garmin_library_cannot_write_into_the_public_log(self):
        try:
            from garminconnect import Garmin, GarminConnectConnectionError
        except ImportError:
            self.skipTest("garminconnect is not installed here")
        g = Garmin(retry_min_wait=0.01, retry_max_wait=0.02)
        g.display_name = "made-up-display-name"

        def unavailable(path, **kwargs):
            raise GarminConnectConnectionError("API Error 503 - Service Unavailable")

        g.client.connectapi = unavailable
        self.fake_session(g)
        code, log = self.run_main("--now", "2026-10-10T10:00:00Z")
        self.assertEqual(code, 1)
        self.assertNotIn("made-up-display-name", log)
        self.assertNotIn("Traceback", log)
        for line in log.strip().splitlines():
            self.assertRegex(line, ALLOWED_LOG)

    def test_garmin_sign_in_failure_sends_a_renewal_note(self):
        _, events = self.fake_session(login_fails=True)
        code, log = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(events, ["login"])
        (server,) = FakeSMTP.instances
        self.assertEqual(len(server.sent), 1)
        self.assertIn("Garmin sign-in did not work", server.sent[0]["Subject"])
        body = server.sent[0].get_body(preferencelist=("plain",)).get_content()
        self.assertIn("garmin_login.py", body)
        self.assertIn("GARMIN_TOKENS", body)

    def test_unexpected_error_reports_where_but_not_what(self):
        data = sample()
        self.fake_session(FakeGarmin(data["responses"]))
        with mock.patch.object(r, "render_html", side_effect=ValueError("resting 48 bpm")):
            code, log = self.run_main("--now", data["now"])
        self.assertEqual(code, 1)
        self.assertIn("stopped by an unexpected error: ValueError", log)
        self.assertNotIn("resting", log)
        self.assertNotIn("bpm", log)


class SessionKeepingTests(unittest.TestCase):
    """garmin_session.py: a refresh token Garmin has already issued is kept
    even when the rest of the sign-in fails."""

    SEED = {"di_token": "access-seed", "di_refresh_token": "refresh-seed", "di_client_id": "client"}
    SAVED = {"di_token": "access-saved", "di_refresh_token": "refresh-saved", "di_client_id": "client"}

    def setUp(self):
        try:
            import cryptography  # noqa: F401
            import garminconnect  # noqa: F401
        except ImportError:
            self.skipTest("garminconnect / cryptography are not installed here")
        import importlib

        sys.modules.pop("garmin_session", None)
        self.gs = importlib.import_module("garmin_session")
        self.addCleanup(sys.modules.pop, "garmin_session", None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "session.enc"
        self.gs.SESSION_FILE = self.file
        self.seed = self.gs._normalise(json.dumps(self.SEED))
        self.file.write_bytes(self.gs._fernet(self.seed).encrypt(json.dumps(self.SAVED, sort_keys=True).encode()) + b"\n")
        env = mock.patch.dict(os.environ, {"GARMIN_TOKENS": json.dumps(self.SEED)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GARMIN_EMAIL", None)
        os.environ.pop("GARMIN_PASSWORD", None)
        self.plan = {}
        plan = self.plan

        class FakeLibrary:
            def __init__(self, *args, **kwargs):
                self.tokens = None
                self.client = types.SimpleNamespace(dumps=lambda: json.dumps(self.tokens))

            def login(self, tokenstore):
                self.tokens = json.loads(tokenstore)
                what = plan[self.tokens["di_refresh_token"]]
                if what == "refresh then fail":
                    self.tokens = dict(self.tokens, di_token="access-new", di_refresh_token="refresh-new")
                if what != "work":
                    raise RuntimeError("profile could not be loaded")

        lib = mock.patch.object(self.gs, "Garmin", FakeLibrary)
        lib.start()
        self.addCleanup(lib.stop)

    def stored(self):
        return json.loads(self.gs._fernet(self.seed).decrypt(self.file.read_bytes().strip()))

    def login(self):
        with contextlib.redirect_stderr(io.StringIO()):
            return self.gs.login()

    def test_new_refresh_token_survives_a_failed_sign_in(self):
        self.plan.update({"refresh-saved": "refresh then fail", "refresh-seed": "fail"})
        with self.assertRaises(SystemExit):
            self.login()
        self.assertEqual(self.stored()["di_refresh_token"], "refresh-new")

    def test_a_plain_failure_leaves_the_saved_session_alone(self):
        before = self.file.read_bytes()
        self.plan.update({"refresh-saved": "fail", "refresh-seed": "fail"})
        with self.assertRaises(SystemExit):
            self.login()
        self.assertEqual(self.file.read_bytes(), before)  # in particular not replaced by the older GARMIN_TOKENS

    def test_normal_sign_in_is_unchanged(self):
        before = self.file.read_bytes()
        self.plan.update({"refresh-saved": "work"})
        g, seed = self.login()
        self.assertEqual(g.tokens["di_refresh_token"], "refresh-saved")
        self.assertEqual(seed, self.seed)
        self.assertFalse(self.gs.save(g, seed))  # nothing rotated, nothing rewritten
        self.assertEqual(self.file.read_bytes(), before)

    def test_falls_back_to_garmin_tokens_when_the_saved_session_fails(self):
        self.plan.update({"refresh-saved": "fail", "refresh-seed": "work"})
        g, _ = self.login()
        self.assertEqual(g.tokens["di_refresh_token"], "refresh-seed")


class SlotTests(unittest.TestCase):
    """in_focus_slots.py: what a run does depending on when GitHub starts it."""

    def plan(self, gmt, asked=None):
        import in_focus_slots

        return in_focus_slots.plan(datetime.fromisoformat(gmt).replace(tzinfo=timezone.utc), asked)

    def test_send_times_are_the_three_dubai_times(self):
        import in_focus_slots

        self.assertEqual(in_focus_slots.SEND_TIMES_DUBAI, {"0730": (7, 30), "1400": (14, 0), "2230": (22, 30)})
        self.assertEqual(r.SEND_SLOTS, ((7, 30), (14, 0), (22, 30)))

    def test_a_run_starting_in_the_hour_before_waits_for_the_send_time(self):
        for gmt, slot, goal in (("2026-10-11T02:27:05", "0730", "2026-10-11T03:30:00"),
                                ("2026-10-11T03:17:40", "0730", "2026-10-11T03:30:00"),
                                ("2026-10-11T08:57:00", "1400", "2026-10-11T10:00:00"),
                                ("2026-10-11T18:29:00", "2230", "2026-10-11T18:30:00")):
            p = self.plan(gmt)
            self.assertEqual((p["slot"], p["day"], p["phase"]), (slot, "2026-10-11", "before"), gmt)
            self.assertEqual(p["goal"], int(datetime.fromisoformat(goal).replace(tzinfo=timezone.utc).timestamp()))

    def test_a_run_starting_at_or_after_the_send_time_is_due(self):
        for gmt, slot in (("2026-10-11T03:29:30", "0730"), ("2026-10-11T03:30:00", "0730"),
                          ("2026-10-11T03:49:00", "0730"), ("2026-10-11T05:00:00", "0730"),
                          ("2026-10-11T10:39:00", "1400"), ("2026-10-11T19:09:00", "2230"),
                          ("2026-10-11T20:00:00", "2230")):  # 22:30 + 90 min is midnight in Dubai
            p = self.plan(gmt)
            self.assertEqual((p["slot"], p["day"], p["phase"]), (slot, "2026-10-11", "due"), gmt)

    def test_a_run_starting_hours_late_does_nothing(self):
        # 21:35 GMT is 01:35 at night in Dubai: the late run of 10 Oct that sent a second evening email
        for gmt in ("2026-10-10T21:35:04", "2026-10-11T05:00:01", "2026-10-11T02:24:59", "2026-10-11T12:00:00",
                    "2026-10-11T20:00:01", "2026-10-11T00:00:00"):
            self.assertEqual(self.plan(gmt)["phase"], "none", gmt)

    def test_every_timer_in_the_workflow_lands_in_a_window(self):
        text = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "garmin-in-focus.yml").read_text()
        crons = re.findall(r'- cron: "([\d,]+) (\d+) \* \* \*"', text)
        self.assertEqual(len(crons), 9)
        seen = {"0730": [], "1400": [], "2230": []}
        for minutes, hour in crons:
            for minute in minutes.split(","):
                p = self.plan(f"2026-10-11T{int(hour):02d}:{int(minute):02d}:00")
                self.assertNotEqual(p["phase"], "none", (hour, minute))
                seen[p["slot"]].append(p["phase"])
        for slot, phases in seen.items():
            self.assertEqual(phases.count("before"), 6, slot)  # six chances to be on time
            self.assertEqual(phases.count("due"), 3, slot)     # three chances to be late rather than never

    def test_asking_for_a_send_time_by_hand(self):
        self.assertEqual(self.plan("2026-10-11T03:50:00", "0730")["phase"], "due")
        self.assertEqual(self.plan("2026-10-11T03:50:00", "1400")["phase"], "none")  # not near 14:00
        import in_focus_slots

        self.assertEqual(in_focus_slots.slot_id("07:30"), "0730")
        self.assertEqual(in_focus_slots.slot_id("2230"), "2230")
        for other in ("", "now", "08:00", None):
            self.assertIsNone(in_focus_slots.slot_id(other))

    def test_command_line_prints_what_the_workflow_reads(self):
        import in_focus_slots

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            in_focus_slots.main(["--slot", "", "--now", "2026-10-11T09:07:00Z"])
        lines = dict(line.split("=", 1) for line in out.getvalue().strip().splitlines())
        self.assertEqual(lines, {"slot": "1400", "day": "2026-10-11", "phase": "before", "goal": "1791712800"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            in_focus_slots.main(["--slot", "tomorrow", "--now", "2026-10-11T09:07:00Z"])
        self.assertIn("phase=none", out.getvalue())


class SentMarkerTests(Base):
    def test_marker_is_written_only_after_an_email_has_really_gone_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, marker = Path(tmp) / "sample.json", Path(tmp) / "marker"
            src.write_text(json.dumps(sample()))
            with mock.patch.dict(os.environ, {"IN_FOCUS_SENT_FILE": str(marker)}):
                FakeSMTP.reject_send = True
                self.run_main("--from-file", str(src))
                self.assertFalse(marker.exists())
                FakeSMTP.reject_send = False
                code, _ = self.run_main("--from-file", str(src))
                self.assertEqual(code, 0)
                self.assertTrue(marker.exists())

    def test_marker_is_also_written_when_only_a_note_went_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, marker = Path(tmp) / "empty.json", Path(tmp) / "marker"
            src.write_text(json.dumps({"date": DAY.isoformat(), "now": "2026-10-10T10:00:00Z",
                                       "responses": {name: None for name in r.CALLS}}))
            with mock.patch.dict(os.environ, {"IN_FOCUS_SENT_FILE": str(marker)}):
                code, _ = self.run_main("--from-file", str(src))
            self.assertEqual(code, 1)
            self.assertTrue(marker.exists())


class RealLibraryTests(unittest.TestCase):
    def test_calls_match_the_installed_garminconnect(self):
        try:
            from garminconnect import Garmin
        except ImportError:
            self.skipTest("garminconnect is not installed here")
        import inspect

        for name in ("get_stats", "get_heart_rates", "get_body_battery", "get_hrv_data", "get_sleep_data",
                     "get_spo2_data", "get_respiration_data", "get_stress_data", "get_steps_data",
                     "get_body_composition"):
            real = list(inspect.signature(getattr(Garmin, name)).parameters)
            fake = list(inspect.signature(getattr(FakeGarmin, name)).parameters)
            self.assertEqual(real, fake, name)


if __name__ == "__main__":
    unittest.main()
