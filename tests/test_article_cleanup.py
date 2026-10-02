import threading
import unittest
from datetime import timedelta
from unittest import mock

from apscheduler.schedulers.background import BackgroundScheduler

import database as db


class ArticleCleanupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (
            mock.patch.object(db, "init_db"),
            mock.patch.object(db, "app_user_count", return_value=1),
            mock.patch.object(BackgroundScheduler, "start"),
        ):
            import app
        cls.module = app

    def test_cleanup_runs_on_worker_start_without_waiting_a_day(self):
        job = self.module.scheduler.get_job("auto_cleanup")
        # A fresh interval job without an explicit next_run_time waits 24 hours.
        self.assertTrue(hasattr(job, "next_run_time"))
        completed = threading.Event()
        scheduler = BackgroundScheduler(timezone=self.module.scheduler.timezone)
        scheduler.add_job(
            self.module._cleanup_job,
            trigger=job.trigger,
            next_run_time=job.next_run_time,
            misfire_grace_time=job.misfire_grace_time,
        )
        with (
            mock.patch.object(db, "delete_old_unpinned_articles",
                              side_effect=lambda **kwargs: completed.set() or 0) as cleanup,
            mock.patch("builtins.print"),
        ):
            scheduler.start()
            try:
                self.assertTrue(completed.wait(timeout=5), "Startup cleanup did not run")
            finally:
                scheduler.shutdown(wait=True)
            cleanup.assert_called_once_with(days=30)

    def test_cleanup_keeps_daily_schedule_and_catches_missed_startup(self):
        job = self.module.scheduler.get_job("auto_cleanup")
        self.assertEqual(job.trigger.interval, timedelta(hours=24))
        self.assertTrue(job.coalesce)
        self.assertEqual(job.max_instances, 1)
        self.assertIsNone(job.misfire_grace_time)

    def test_empty_cleanup_is_logged_for_operational_checks(self):
        with (
            mock.patch.object(db, "delete_old_unpinned_articles", return_value=0),
            mock.patch("builtins.print") as log,
        ):
            self.module._cleanup_job()
        log.assert_called_once_with(
            "[Scheduler] 0 alte ungepinnte Artikel gelöscht.", flush=True,
        )


if __name__ == "__main__":
    unittest.main()
