import unittest
from unittest import mock

from apscheduler.schedulers.background import BackgroundScheduler
from psycopg2 import OperationalError

import database as db


class AvailabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (
            mock.patch.object(db, "init_db"),
            mock.patch.object(db, "app_user_count", return_value=1),
            mock.patch.object(BackgroundScheduler, "start"),
        ):
            import app
        cls.module = app

    def test_anonymous_login_and_redirect_need_no_database_or_ai(self):
        with (
            mock.patch.object(db, "get_auth_user_state", side_effect=AssertionError("DB called")),
            mock.patch.object(self.module.ai, "is_configured", side_effect=AssertionError("AI called")),
        ):
            client = self.module.app.test_client()
            response = client.get("/", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="username"', response.data)
        self.assertNotIn(b"disabled", response.data)
        self.assertNotIn(b"keine Nutzer eingerichtet", response.data)

    def test_login_submission_still_authenticates(self):
        users = {"alice": {"password": "test-password", "role": "viewer"}}
        with mock.patch.object(db, "get_auth_user_state", return_value=(1, users)) as load:
            client = self.module.app.test_client()
            response = client.post("/login", data={"username": "alice", "password": "test-password"})
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            self.assertEqual(session["username"], "alice")
        load.assert_called_once()

    def test_database_error_returns_503_without_retrying_in_template(self):
        with (
            mock.patch.object(db, "get_auth_user_state", side_effect=OperationalError("unavailable")) as load,
            mock.patch.object(self.module, "_load_env_users", side_effect=AssertionError("auth fallback")),
            mock.patch.object(self.module.ai, "is_configured", side_effect=AssertionError("AI called")),
            mock.patch.object(self.module.app.logger, "exception"),
        ):
            response = self.module.app.test_client().post("/login", data={"username": "alice"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Retry-After"], "10")
        self.assertIn("vorübergehend".encode(), response.data)
        load.assert_called_once()

    def test_api_database_error_is_json(self):
        client = self.module.app.test_client()
        with client.session_transaction() as session:
            session["username"] = "alice"
        with (
            mock.patch.object(db, "get_auth_user_state", side_effect=OperationalError("unavailable")),
            mock.patch.object(self.module.app.logger, "exception"),
        ):
            response = client.get("/api/assistant/status")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json["ok"])

    def test_empty_user_table_still_explains_setup_on_submission(self):
        with (
            mock.patch.object(db, "get_auth_user_state", return_value=(0, {})),
            mock.patch.object(self.module, "_load_env_users", return_value={}),
        ):
            response = self.module.app.test_client().post("/login", data={"username": "alice"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"disabled", response.data)
        self.assertIn(b"keine Nutzer eingerichtet", response.data)

    def test_auth_reuses_query_within_request_but_refreshes_between_requests(self):
        user = {"role": "admin", "travel_health_access": "none"}
        with mock.patch.object(db, "get_auth_user_state", side_effect=[(1, {"alice": user}), (1, {})]) as load:
            with self.module.app.test_request_context("/"):
                self.module.session["username"] = "alice"
                self.assertTrue(self.module.can_admin())
                self.assertEqual(self.module.available_workspaces(), ["core", "travel_health"])
                self.module.current_user()
                load.assert_called_once()
            with self.module.app.test_request_context("/"):
                self.module.session["username"] = "alice"
                self.assertIsNone(self.module.current_user())
        self.assertEqual(load.call_count, 2)

    def test_health_checks_database_without_authentication(self):
        with mock.patch.object(db, "get_db") as get_db:
            response = self.module.app.test_client().get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"status": "ok"})
        get_db.return_value.__enter__.return_value.execute.assert_called_once_with("SELECT 1")

    def test_health_fails_on_database_outage(self):
        with (
            mock.patch.object(db, "get_db", side_effect=OperationalError("unavailable")),
            mock.patch.object(self.module.app.logger, "exception"),
        ):
            response = self.module.app.test_client().get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json, {"status": "unavailable"})


class DatabaseTimeoutTest(unittest.TestCase):
    def test_all_connection_paths_use_timeouts_and_keepalives(self):
        raw = mock.MagicMock()
        raw.cursor.return_value.__enter__.return_value.__iter__.return_value = iter([])
        with (
            mock.patch.object(db, "DATABASE_URL", "postgresql://localhost/test?options=-csearch_path%3Dpublic"),
            mock.patch.dict(db.os.environ, {
                "DB_CONNECT_TIMEOUT_SECONDS": "5",
                "DB_STATEMENT_TIMEOUT_MS": "30000",
                "DB_LOCK_TIMEOUT_MS": "5000",
            }),
            mock.patch.object(db.psycopg2, "connect", return_value=raw) as connect,
        ):
            with db.get_db():
                pass
            with db.assistant_index_lock():
                pass
            list(db.iter_article_chunks_for_pinned([1]))
        self.assertEqual(connect.call_count, 3)
        for call in connect.call_args_list:
            self.assertEqual(call.kwargs["connect_timeout"], 5)
            self.assertIn("-csearch_path=public", call.kwargs["options"])
            self.assertIn("statement_timeout=30000", call.kwargs["options"])
            self.assertIn("lock_timeout=5000", call.kwargs["options"])
            self.assertEqual(call.kwargs["keepalives"], 1)
        self.assertEqual(raw.close.call_count, 3)


if __name__ == "__main__":
    unittest.main()
