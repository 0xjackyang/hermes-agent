"""Auto-repair for a corrupt kanban DB (unclean-shutdown WAL desync).

Critical safety properties:
  * a clobbered kanban header must NOT crash the dispatcher — connect()
    recovers in place and returns a usable, schema'd connection;
  * only CORRUPTION is recovered — ordinary sqlite errors still propagate,
    so a transient/locked DB is never destructively rebuilt;
  * a healthy DB is opened as-is, never spuriously rebuilt.

state.db is intentionally out of scope: this repair lives only in
hermes_cli.kanban_db.connect(), so durable conversation history is never
reachable from the rebuild path.
"""
import sqlite3
from pathlib import Path

from hermes_cli.kanban_db import (
    connect,
    _is_kanban_corruption,
    _INITIALIZED_PATHS,
)


def _kanban_path(tmp_path) -> Path:
    return tmp_path / "kanban.db"


class TestKanbanCorruptionDetection:
    def test_corruption_markers_match(self):
        assert _is_kanban_corruption(
            sqlite3.DatabaseError("file is not a database")
        )
        assert _is_kanban_corruption(
            sqlite3.DatabaseError("database disk image is malformed")
        )

    def test_non_corruption_not_matched(self):
        # ordinary errors must NOT be treated as corruption -> never nuke
        assert not _is_kanban_corruption(
            sqlite3.OperationalError("database is locked")
        )
        assert not _is_kanban_corruption(
            sqlite3.OperationalError("no such table: tasks")
        )
        assert not _is_kanban_corruption(ValueError("unrelated"))


class TestKanbanAutoRepair:
    def test_clobbered_header_is_rebuilt(self, tmp_path):
        db = _kanban_path(tmp_path)
        # Invalid SQLite header -> "file is not a database" on first access.
        db.write_bytes(b"NOT a sqlite database -- header clobbered\x00" * 8)
        _INITIALIZED_PATHS.discard(str(db.resolve()))
        # connect() must recover (rebuild) rather than raise.
        conn = connect(db_path=db)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "tasks" in tables  # schema rebuilt
            assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        finally:
            conn.close()

    def test_healthy_db_not_spuriously_rebuilt(self, tmp_path):
        db = _kanban_path(tmp_path)
        _INITIALIZED_PATHS.discard(str(db.resolve()))
        c1 = connect(db_path=db)
        try:
            # custom sentinel table survives CREATE TABLE IF NOT EXISTS but
            # would be lost by a (wrong) rebuild.
            c1.execute("CREATE TABLE _repair_sentinel (x INTEGER)")
            c1.execute("INSERT INTO _repair_sentinel VALUES (1)")
        finally:
            c1.close()
        _INITIALIZED_PATHS.discard(str(db.resolve()))
        c2 = connect(db_path=db)
        try:
            assert (
                c2.execute("SELECT count(*) FROM _repair_sentinel").fetchone()[0]
                == 1
            )
        finally:
            c2.close()
