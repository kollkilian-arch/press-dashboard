import unittest
from unittest import mock

import database as db
import memory_budget


class ContainerMemoryTest(unittest.TestCase):
    def test_reads_cgroup_v2(self):
        with mock.patch.object(memory_budget.Path, "read_text", side_effect=["123\n", "512\n"]):
            self.assertEqual(memory_budget.container_memory(), {"used_bytes": 123, "limit_bytes": 512})

    def test_falls_back_to_cgroup_v1(self):
        with mock.patch.object(memory_budget.Path, "read_text", side_effect=[FileNotFoundError(), "123", "512"]):
            self.assertEqual(memory_budget.container_memory(), {"used_bytes": 123, "limit_bytes": 512})

    def test_missing_or_unlimited_cgroup_does_not_pause(self):
        with mock.patch.object(memory_budget.Path, "read_text", side_effect=["123", "max", "123", str(2**63)]):
            self.assertIsNone(memory_budget.container_memory())
        self.assertFalse(memory_budget.indexing_should_pause(None))

    def test_pause_has_hysteresis(self):
        self.assertTrue(memory_budget.indexing_should_pause({"used_bytes": 360, "limit_bytes": 512}))
        self.assertFalse(memory_budget.indexing_should_pause({"used_bytes": 330, "limit_bytes": 512}))
        self.assertTrue(memory_budget.indexing_should_pause({"used_bytes": 330, "limit_bytes": 512}, already_paused=True))
        self.assertFalse(memory_budget.indexing_should_pause({"used_bytes": 300, "limit_bytes": 512}, already_paused=True))


class StreamingVectorsTest(unittest.TestCase):
    def test_uses_server_cursor_and_closes_on_early_exit(self):
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.__iter__.return_value = iter([{"article_id": 1}, {"article_id": 2}])
        with mock.patch.object(db.psycopg2, "connect", return_value=connection):
            rows = db.iter_article_chunks_for_pinned([1, 2])
            self.assertEqual(next(rows), {"article_id": 1})
            self.assertEqual(cursor.itersize, 8)
            self.assertEqual(connection.cursor.call_args.kwargs["name"], "assistant_vectors")
            cursor.fetchall.assert_not_called()
            rows.close()
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
