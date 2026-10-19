import importlib.util
from pathlib import Path
import types
import unittest
from unittest import mock

from psycopg2.errors import DuplicateColumn, LockNotAvailable

import database as db


class SchemaStartupTest(unittest.TestCase):
    def setUp(self):
        self.raw = mock.MagicMock()
        self.cursor = self.raw.cursor.return_value
        # Existing data and legacy migration markers, so no seed rows are needed.
        self.cursor.fetchone.return_value = {"name": "settings", "value": "old", "n": 1}
        self.cursor.fetchall.return_value = []
        self.connection = mock.patch.object(db, "_connect_db", return_value=self.raw)
        self.connection.start()
        self.addCleanup(self.connection.stop)

    def statements(self):
        return [call.args[0] for call in self.cursor.execute.call_args_list]

    def test_restart_with_current_schema_does_not_take_table_locks_or_update_articles(self):
        self.cursor.fetchone.return_value = {"name": "settings", "value": db.SCHEMA_VERSION}
        db.init_db()
        statements = self.statements()
        self.assertEqual(len(statements), 3)
        self.assertTrue(all(sql.startswith("SELECT ") for sql in statements))
        self.assertIn("pg_advisory_xact_lock", statements[0])
        self.raw.commit.assert_called_once()
        self.raw.close.assert_called_once()

    def test_upgrade_publishes_version_last_in_the_same_transaction(self):
        db.init_db()
        self.assertTrue(any("ALTER TABLE articles" in sql for sql in self.statements()))
        final_call = self.cursor.execute.call_args_list[-1]
        self.assertIn("ON CONFLICT (key) DO UPDATE", final_call.args[0])
        self.assertEqual(final_call.args[1], (db.SCHEMA_VERSION_KEY, db.SCHEMA_VERSION))
        self.raw.commit.assert_called_once()

    def test_first_setup_without_settings_table_runs_schema_creation(self):
        self.cursor.fetchone.side_effect = [
            {"name": None},
            *[{"n": 1}] * (len(db.MANAGED_SOURCE_MIGRATIONS) + 5),
        ]
        db.init_db()
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS settings" in sql for sql in self.statements()))
        self.raw.commit.assert_called_once()

    def test_lock_timeout_rolls_back_instead_of_marking_schema_ready(self):
        def execute(sql, params=()):
            if sql.startswith("ALTER TABLE articles ADD COLUMN ai_summary"):
                raise LockNotAvailable("table busy")

        self.cursor.execute.side_effect = execute
        with self.assertRaises(LockNotAvailable):
            db.init_db()
        self.raw.commit.assert_not_called()
        self.raw.rollback.assert_called_once()
        self.raw.close.assert_called_once()
        self.assertNotIn((db.SCHEMA_VERSION_KEY, db.SCHEMA_VERSION),
                         [call.args[1] for call in self.cursor.execute.call_args_list if len(call.args) > 1])

    def test_existing_legacy_columns_are_still_allowed(self):
        def execute(sql, params=()):
            if sql.startswith("ALTER TABLE articles ADD COLUMN ai_summary"):
                raise DuplicateColumn("column already exists")

        self.cursor.execute.side_effect = execute
        db.init_db()
        self.assertIn("ROLLBACK TO SAVEPOINT add_col", self.statements())
        self.raw.commit.assert_called_once()


class GunicornDiagnosticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "gunicorn.conf.py"
        spec = importlib.util.spec_from_file_location("startup_gunicorn_config", path)
        cls.config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.config)

    def test_completed_request_cancels_slow_request_diagnostic(self):
        worker = mock.Mock()
        req = types.SimpleNamespace(method="GET", path="/login", query="secret=value")
        with mock.patch.object(self.config.threading, "Timer") as timer:
            self.config.pre_request(worker, req)
            self.config.post_request(worker, req, {}, None)
        timer.return_value.start.assert_called_once()
        timer.return_value.cancel.assert_called_once()
        self.assertEqual(timer.call_args.kwargs["args"], (worker, "GET", "/login"))

    def test_stuck_request_records_stacks_before_request_completes(self):
        worker = mock.Mock()
        with mock.patch.object(self.config.faulthandler, "dump_traceback") as dump:
            self.config._log_slow_request(worker, "GET", "/")
        worker.log.warning.assert_called_once()
        dump.assert_called_once_with(file=self.config.sys.stderr, all_threads=True)


if __name__ == "__main__":
    unittest.main()
