import json
import unittest
from contextlib import nullcontext
from unittest import mock

import ai
import assistant_index as index
import database as db


def batch_result(**changes):
    return {
        "checked_articles": 5, "refreshed_articles": 5, "embedded_chunks": 10,
        "failed_articles": 0, "last_article_id": 12, "has_more": True,
        "embedding_model": "test-model", **changes,
    }


class BackgroundIndexTest(unittest.TestCase):
    def setUp(self):
        self.settings = {}
        for patcher in (
            mock.patch.object(db, "get_setting", side_effect=lambda key, default="": self.settings.get(key, default)),
            mock.patch.object(db, "set_setting", side_effect=lambda key, value: self.settings.__setitem__(key, value)),
            mock.patch.object(db, "count_pinned_assistant_articles", return_value=20),
            mock.patch.object(db, "assistant_index_lock", side_effect=lambda: nullcontext(True)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_manual_request_only_queues_and_is_visible_to_another_worker(self):
        with mock.patch.object(ai, "refresh_pinned_article_chunks") as refresh:
            state = index.request_refresh()
            self.assertEqual(state["status"], "queued")
            self.assertEqual(index.get_status()["status"], "queued")
            refresh.assert_not_called()

    def test_automatic_pass_resumes_persisted_cursor_and_completes(self):
        with mock.patch.object(ai, "refresh_pinned_article_chunks", return_value=batch_result()) as refresh:
            index.run_batch()
        refresh.assert_called_once_with(after_article_id=0)
        self.assertEqual(index.get_status()["last_article_id"], 12)
        # Simulate the next tick in a different worker, using only saved state.
        with (
            mock.patch.object(index.time, "time", return_value=index.time.time() + 60),
            mock.patch.object(ai, "refresh_pinned_article_chunks", return_value=batch_result(last_article_id=20, has_more=False)) as refresh,
        ):
            index.run_batch()
        refresh.assert_called_once_with(after_article_id=12)
        state = index.get_status()
        self.assertEqual(state["status"], "idle")
        self.assertEqual(state["refreshed_articles"], 10)
        self.assertEqual(state["embedded_chunks"], 20)

    def test_concurrent_worker_skips_indexing(self):
        with (
            mock.patch.object(db, "assistant_index_lock", return_value=nullcontext(False)),
            mock.patch.object(ai, "refresh_pinned_article_chunks") as refresh,
        ):
            index.run_batch()
        refresh.assert_not_called()
        self.assertEqual(self.settings, {})

    def test_worker_restart_recovers_running_job(self):
        self.settings[index.STATE_KEY] = json.dumps({
            "status": "running", "has_more": True, "last_article_id": 18,
            "checked_articles": 4, "refreshed_articles": 2, "embedded_chunks": 3,
            "failed_articles": 0, "updated_at": 1,
        })
        with mock.patch.object(ai, "refresh_pinned_article_chunks", return_value=batch_result()) as refresh:
            index.run_batch()
        refresh.assert_called_once_with(after_article_id=18)

    def test_error_preserves_cursor_for_automatic_retry(self):
        self.settings[index.STATE_KEY] = json.dumps({
            "status": "running", "has_more": True, "last_article_id": 18,
            "checked_articles": 4, "refreshed_articles": 2, "embedded_chunks": 3,
            "failed_articles": 0, "updated_at": 1,
        })
        with mock.patch.object(ai, "refresh_pinned_article_chunks", side_effect=RuntimeError("temporary")), self.assertLogs(index.logger):
            index.run_batch()
        state = index.get_status()
        self.assertEqual(state["status"], "error")
        self.assertTrue(state["has_more"])
        self.assertEqual(state["last_article_id"], 18)

    def test_idle_cooldown_and_manual_request_starts_new_pass(self):
        with mock.patch.object(ai, "refresh_pinned_article_chunks", return_value=batch_result(has_more=False)) as refresh:
            index.run_batch()
            index.run_batch()
            self.assertEqual(refresh.call_count, 1)
            index.request_refresh()
            index.run_batch()
            self.assertEqual(refresh.call_count, 2)
            refresh.assert_called_with(after_article_id=0)


class IndexLockTest(unittest.TestCase):
    def test_connection_closes_and_releases_lock_on_worker_error(self):
        connection = mock.MagicMock()
        connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (True,)
        with mock.patch.object(db.psycopg2, "connect", return_value=connection):
            with self.assertRaises(RuntimeError):
                with db.assistant_index_lock() as acquired:
                    self.assertTrue(acquired)
                    raise RuntimeError("interrupted")
        connection.close.assert_called_once()


class IndexEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from apscheduler.schedulers.background import BackgroundScheduler
        with (
            mock.patch.object(db, "init_db"),
            mock.patch.object(db, "app_user_count", return_value=1),
            mock.patch.object(BackgroundScheduler, "start"),
        ):
            import app
        cls.module = app

    def test_post_returns_202_without_calling_provider_and_status_is_readable(self):
        user = {"username": "editor", "role": "editor"}
        with (
            mock.patch.object(self.module, "current_user", return_value=user),
            mock.patch.object(self.module, "can_edit", return_value=True),
            mock.patch.object(index, "request_refresh", return_value={"status": "queued"}),
            mock.patch.object(index, "get_status", return_value={"status": "running", "checked_articles": 5}),
            mock.patch.object(ai, "refresh_pinned_article_chunks") as refresh,
        ):
            client = self.module.app.test_client()
            response = client.post("/api/assistant/reindex")
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json, {"ok": True, "status": "queued"})
            status = client.get("/api/assistant/reindex/status")
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json["checked_articles"], 5)
            refresh.assert_not_called()

    def test_viewer_cannot_request_indexing(self):
        with (
            mock.patch.object(self.module, "current_user", return_value={"username": "viewer", "role": "viewer"}),
            mock.patch.object(self.module, "can_edit", return_value=False),
            mock.patch.object(index, "request_refresh") as request,
        ):
            response = self.module.app.test_client().post("/api/assistant/reindex")
        self.assertEqual(response.status_code, 403)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
