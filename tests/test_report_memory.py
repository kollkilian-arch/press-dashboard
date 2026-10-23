import json
import unittest
from unittest import mock

from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup

import ai
import database as db


class ReportMemoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (
            mock.patch.object(db, "init_db"),
            mock.patch.object(db, "app_user_count", return_value=1),
            mock.patch.object(BackgroundScheduler, "start"),
        ):
            import app
        cls.module = app

    def setUp(self):
        self.previous = {"date": "2026-10-16", "report": {
            "action_title": "Altersvorsorge-Reform", "zusammenfassung": "Bisherige Pläne.",
            "abschnitte": [], "top_themen": ["Vorsorge"],
        }}
        self.user = {"username": "editor", "role": "editor"}

    def test_create_snapshots_previous_report_only_when_enabled_for_weekly_mode(self):
        for mode, checked in (("weekly", True), ("weekly", False), ("daily", True)):
            enabled = mode == "weekly" and checked
            with (
                self.subTest(mode=mode, checked=checked),
                mock.patch.object(self.module, "current_user", return_value=self.user),
                mock.patch.object(ai, "is_configured", return_value=True),
                mock.patch.object(db, "get_articles_for_week_report", return_value=[{"id": 1}]),
                mock.patch.object(db, "get_articles_for_report", return_value=[{"id": 1}]),
                mock.patch.object(self.module, "_previous_week_report_context", return_value=self.previous) as previous,
                mock.patch.object(db, "create_report_job") as create,
                mock.patch.object(self.module, "_start_report_job") as start,
            ):
                form = {"mode": mode, "date": "2026-10-23"}
                if checked:
                    form["use_previous_report"] = "1"
                response = self.module.app.test_client().post("/bericht/erstellen", data=form)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(create.call_args.kwargs, {
                "use_previous_report": enabled,
                "previous_report": self.previous if enabled else None,
            })
            self.assertEqual(previous.call_count, int(enabled))
            start.assert_called_once_with(create.call_args.args[0])

    def test_worker_uses_snapshot_and_records_actual_context(self):
        for enabled, context in ((True, self.previous), (True, None), (False, self.previous)):
            with (
                self.subTest(enabled=enabled, context=bool(context)),
                mock.patch.object(db, "get_report_job", return_value={
                    "mode": "weekly", "target_date": "2026-10-23",
                    "report_key": "2026-10-23_weekly", "period_label": "2026-10-17 bis 2026-10-23",
                    "use_previous_report": int(enabled), "previous_report_json": json.dumps(context),
                }),
                mock.patch.object(db, "mark_report_job_running"),
                mock.patch.object(db, "get_articles_for_week_report", return_value=[{"id": 1}]),
                mock.patch.object(ai, "is_configured", return_value=True),
                mock.patch.object(ai, "generate_daily_report", return_value={"zusammenfassung": "Neu"}) as generate,
                mock.patch.object(db, "save_report") as save,
                mock.patch.object(db, "mark_report_job_succeeded") as succeeded,
                mock.patch.object(db, "mark_report_job_failed") as failed,
                mock.patch.object(db, "get_previous_week_report") as lookup,
            ):
                self.module._run_report_job("job-1")
            failed.assert_not_called()
            lookup.assert_not_called()
            self.assertEqual(generate.call_args.kwargs["previous_report"], context if enabled else None)
            self.assertEqual(json.loads(save.call_args.args[1])["memory"], {
                "enabled": enabled, "previous_report_date": "2026-10-16" if enabled and context else None,
            })
            if enabled and context is None:
                self.assertIn("Kein Vorwochenbericht verfügbar", succeeded.call_args.args[1])

    def test_context_handles_missing_or_unreadable_reports(self):
        for content in (None, "not json", "[]", "{}"):
            row = {"date": "2026-10-16_weekly", "content": content} if content is not None else None
            with self.subTest(content=content), mock.patch.object(db, "get_previous_week_report", return_value=row):
                self.assertIsNone(self.module._previous_week_report_context("2026-10-23"))
        with mock.patch.object(db, "get_previous_week_report", return_value={
            "date": "2026-10-16_weekly", "content": json.dumps(self.previous["report"]),
        }):
            self.assertEqual(self.module._previous_week_report_context("2026-10-23"), self.previous)

    def test_weekly_page_exposes_switch_and_missing_context(self):
        for mode, context in (("weekly", self.previous), ("weekly", None), ("daily", None)):
            with (
                self.subTest(mode=mode, context=bool(context)),
                mock.patch.object(self.module, "current_user", return_value=self.user),
                mock.patch.object(ai, "is_configured", return_value=True),
                mock.patch.object(db, "get_report", return_value=None),
                mock.patch.object(db, "get_recent_reports", return_value=[]),
                mock.patch.object(self.module, "_previous_week_report_context", return_value=context),
            ):
                response = self.module.app.test_client().get(f"/bericht?mode={mode}&date=2026-10-23")
            self.assertEqual(response.status_code, 200)
            soup = BeautifulSoup(response.data, "html.parser")
            switch = soup.find(id="report-memory")
            if mode == "weekly":
                self.assertEqual(switch["name"], "use_previous_report")
                self.assertNotIn("checked", switch.attrs)
                self.assertEqual(switch.find_parent("form")["action"], "/bericht/erstellen")
                help_text = soup.find(id="report-memory-help")
                self.assertIn("visually-hidden", help_text["class"])
                self.assertEqual(switch.parent["title"], help_text.get_text())
                self.assertIn("2026-10-16" if context else "Kein Vorwochenbericht verfügbar", switch.parent["title"])
            else:
                self.assertIsNone(switch)

    def test_switch_restores_saved_choice_and_locks_while_job_runs(self):
        report = {"zusammenfassung": "Neue Pläne.", "abschnitte": [], "memory": {
            "enabled": True, "previous_report_date": "2026-10-16",
        }}
        for running in (False, True):
            job = {"id": "job-1", "mode": "weekly", "target_date": "2026-10-23",
                   "status": "running", "use_previous_report": 1, "message": "Läuft"} if running else None
            with (
                self.subTest(running=running),
                mock.patch.object(self.module, "current_user", return_value=self.user),
                mock.patch.object(ai, "is_configured", return_value=True),
                mock.patch.object(ai, "attach_report_references", side_effect=lambda report, *args, **kwargs: report),
                mock.patch.object(db, "get_report", return_value={
                    "content": json.dumps(report), "article_count": 1, "created_at": "2026-10-23 10:00:00",
                }),
                mock.patch.object(db, "get_report_job", return_value=job),
                mock.patch.object(db, "get_articles_for_week_report", return_value=[]),
                mock.patch.object(db, "get_recent_reports", return_value=[]),
                mock.patch.object(self.module, "_previous_week_report_context", return_value=self.previous),
            ):
                response = self.module.app.test_client().get("/bericht?mode=weekly&date=2026-10-23&job=job-1")
            self.assertEqual(response.status_code, 200)
            soup = BeautifulSoup(response.data, "html.parser")
            switch = soup.find(id="report-memory")
            self.assertIn("checked", switch.attrs)
            self.assertEqual("disabled" in switch.attrs, running)
            self.assertIn("Mit Gedächtnis", soup.get_text())
            self.assertIsNotNone(soup.find("a", href="/bericht?mode=weekly&date=2026-10-16"))


class ReportMemoryDatabaseTest(unittest.TestCase):
    def test_selection_excludes_current_overlapping_and_stale_weeks(self):
        with mock.patch.object(db, "get_db") as get_db:
            conn = get_db.return_value.__enter__.return_value
            db.get_previous_week_report("2026-10-23")
        sql, params = conn.execute.call_args.args
        self.assertEqual(params, ("2026-10-10_weekly", "2026-10-16_weekly"))
        self.assertIn("RIGHT(date, 7) = '_weekly'", sql)
        self.assertIn("ORDER BY date DESC LIMIT 1", sql)

    def test_job_persists_option_and_context_snapshot(self):
        previous = {"date": "2026-10-16", "report": {"zusammenfassung": "Pläne"}}
        with mock.patch.object(db, "get_db") as get_db:
            conn = get_db.return_value.__enter__.return_value
            db.create_report_job("job-1", "weekly", "2026-10-23", "2026-10-23_weekly", "Woche", 4,
                                 use_previous_report=True, previous_report=previous)
        sql, params = conn.execute.call_args.args
        self.assertIn("use_previous_report, previous_report_json", sql)
        self.assertEqual(params[6], 1)
        self.assertEqual(json.loads(params[7]), previous)


if __name__ == "__main__":
    unittest.main()
