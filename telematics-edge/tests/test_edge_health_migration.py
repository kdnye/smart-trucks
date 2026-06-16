import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

import aiosqlite

MODULE_PATH = Path(__file__).resolve().parents[1] / "db.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("telematics_edge_db", MODULE_PATH)
db = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(db)

# Pre-migration edge_health schema: no sent_at_utc / attempt_count columns.
OLD_EDGE_HEALTH = """
CREATE TABLE edge_health (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at_utc TEXT NOT NULL,
    queue_depth     INTEGER NOT NULL,
    payload_json    TEXT NOT NULL
);
"""


class EdgeHealthMigrationTests(unittest.TestCase):
    def test_ensure_column_adds_sync_columns_idempotently(self) -> None:
        async def scenario() -> None:
            path = tempfile.mktemp(suffix=".db")
            conn = await aiosqlite.connect(path)
            try:
                await conn.execute(OLD_EDGE_HEALTH)
                await conn.execute(
                    "INSERT INTO edge_health (captured_at_utc, queue_depth, payload_json) "
                    "VALUES ('2026-06-16T00:00:00Z', 0, '{}')"
                )
                await conn.commit()

                # The sync-service query fails on the old schema.
                with self.assertRaises(aiosqlite.OperationalError):
                    await conn.execute("SELECT id FROM edge_health WHERE sent_at_utc IS NULL")

                # Apply the migration twice to prove idempotency.
                for _ in range(2):
                    await db._ensure_column(conn, "edge_health", "sent_at_utc", "TEXT")
                    await db._ensure_column(
                        conn, "edge_health", "attempt_count", "INTEGER NOT NULL DEFAULT 0"
                    )
                await conn.commit()

                async with conn.execute("PRAGMA table_info(edge_health)") as cursor:
                    columns = {row[1] for row in await cursor.fetchall()}
                self.assertIn("sent_at_utc", columns)
                self.assertIn("attempt_count", columns)

                # Full sync-service workflow now succeeds.
                async with conn.execute(
                    "SELECT id, captured_at_utc, payload_json FROM edge_health WHERE sent_at_utc IS NULL"
                ) as cursor:
                    rows = await cursor.fetchall()
                self.assertEqual(len(rows), 1)
                row_id = rows[0][0]
                await conn.execute(
                    "UPDATE edge_health SET sent_at_utc = CURRENT_TIMESTAMP WHERE id IN (?)", (row_id,)
                )
                await conn.execute(
                    "UPDATE edge_health SET attempt_count = attempt_count + 1 WHERE id IN (?)", (row_id,)
                )
                await conn.commit()

                async with conn.execute(
                    "SELECT attempt_count FROM edge_health WHERE sent_at_utc IS NOT NULL"
                ) as cursor:
                    sent = await cursor.fetchall()
                self.assertEqual(sent, [(1,)])
            finally:
                await conn.close()
                os.unlink(path)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
