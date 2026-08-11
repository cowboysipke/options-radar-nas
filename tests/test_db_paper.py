import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from options_radar.db import Database
from options_radar.models import RawMessage


class DatabaseTests(unittest.TestCase):
    def test_message_deduplication_and_paper_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "test.db")
            message = RawMessage(channel="pa分析师", analyst="pa", observed_at=datetime(2026, 8, 6), content="same")
            first, created_first = database.insert_raw_message(message)
            second, created_second = database.insert_raw_message(message)
            self.assertEqual(first, second)
            self.assertTrue(created_first)
            self.assertFalse(created_second)
            self.assertEqual(database.paper_stats()["closed"], 0)


if __name__ == "__main__":
    unittest.main()
