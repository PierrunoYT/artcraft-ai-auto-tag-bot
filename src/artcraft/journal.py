"""Action journal: what the bot did, so it can resume runs, catch up after downtime,
and undo its own changes.

Stores only IDs, statuses, and timestamps (post, tag, message, and user IDs), never
titles or message text.
"""

import contextvars
import datetime
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

# The run the current task belongs to; asyncio tasks inherit it, so actions taken
# inside a run's workers are attributed to that run automatically.
current_run: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_run", default=None
)


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def stamp(moment: datetime.datetime) -> str:
    return moment.isoformat(timespec="seconds")


@dataclass(frozen=True)
class Run:
    id: int
    kind: str
    forum_id: int
    options: dict
    user_id: int | None
    started: str
    finished: str | None
    status: str


@dataclass(frozen=True)
class Action:
    id: int
    at: str
    run_id: int | None
    kind: str
    thread_id: int
    before: Any
    after: Any
    message_id: int | None
    undone: bool


class Journal:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                forum_id INTEGER NOT NULL,
                options TEXT NOT NULL,
                user_id INTEGER,
                started TEXT NOT NULL,
                finished TEXT,
                status TEXT NOT NULL
            );
            -- Posts a run has finished, so an interrupted run can resume.
            CREATE TABLE IF NOT EXISTS run_items (
                run_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                data TEXT,
                PRIMARY KEY (run_id, thread_id)
            );
            -- Changes the bot made, with what is needed to undo them.
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                run_id INTEGER,
                kind TEXT NOT NULL,
                thread_id INTEGER NOT NULL,
                before TEXT,
                after TEXT,
                message_id INTEGER,
                undone INTEGER NOT NULL DEFAULT 0
            );
            -- New posts already handled, so catch-up never handles one twice.
            CREATE TABLE IF NOT EXISTS handled_posts (
                thread_id INTEGER PRIMARY KEY,
                at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        self.db.commit()

    # Runs

    def start_run(self, kind: str, forum_id: int, options: dict, user_id: int | None) -> int:
        cursor = self.db.execute(
            "INSERT INTO runs (kind, forum_id, options, user_id, started, status) "
            "VALUES (?, ?, ?, ?, ?, 'running')",
            (kind, forum_id, json.dumps(options), user_id, stamp(now())),
        )
        self.db.commit()
        return cursor.lastrowid

    def finish_run(self, run_id: int, status: str) -> None:
        self.db.execute(
            "UPDATE runs SET status = ?, finished = ? WHERE id = ?",
            (status, stamp(now()), run_id),
        )
        self.db.commit()

    def _run(self, row) -> Run:
        return Run(row[0], row[1], row[2], json.loads(row[3]), *row[4:])

    def run(self, run_id: int) -> Run | None:
        row = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._run(row) if row else None

    def runs(self, limit: int = 10) -> list[Run]:
        rows = self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [self._run(row) for row in rows]

    def interrupted_runs(self) -> list[Run]:
        """Runs still marked running: the bot stopped before they finished."""
        rows = self.db.execute("SELECT * FROM runs WHERE status = 'running' ORDER BY id")
        return [self._run(row) for row in rows]

    def record_item(self, run_id: int, thread_id: int, status: str, data: Any = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO run_items VALUES (?, ?, ?, ?)",
            (run_id, thread_id, status, None if data is None else json.dumps(data)),
        )
        self.db.commit()

    def items(self, run_id: int) -> dict[int, tuple[str, Any]]:
        rows = self.db.execute(
            "SELECT thread_id, status, data FROM run_items WHERE run_id = ?", (run_id,)
        )
        return {t: (s, None if d is None else json.loads(d)) for t, s, d in rows}

    def item_counts(self, run_id: int) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT status, COUNT(*) FROM run_items WHERE run_id = ? GROUP BY status", (run_id,)
        )
        return dict(rows.fetchall())

    # Actions

    def record(
        self,
        kind: str,
        thread_id: int,
        before: Any = None,
        after: Any = None,
        message_id: int | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO actions (at, run_id, kind, thread_id, before, after, message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                stamp(now()),
                current_run.get(),
                kind,
                thread_id,
                None if before is None else json.dumps(before),
                None if after is None else json.dumps(after),
                message_id,
            ),
        )
        self.db.commit()

    def actions(
        self,
        run_id: int | None = None,
        since: datetime.datetime | None = None,
        thread_id: int | None = None,
        include_undone: bool = False,
        limit: int | None = None,
    ) -> list[Action]:
        """Matching actions, newest first."""
        where, params = [], []
        if run_id is not None:
            where.append("run_id = ?")
            params.append(run_id)
        if since is not None:
            where.append("at >= ?")
            params.append(stamp(since))
        if thread_id is not None:
            where.append("thread_id = ?")
            params.append(thread_id)
        if not include_undone:
            where.append("undone = 0")
        sql = "SELECT * FROM actions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [
            Action(
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
                None if row[5] is None else json.loads(row[5]),
                None if row[6] is None else json.loads(row[6]),
                row[7],
                bool(row[8]),
            )
            for row in self.db.execute(sql, params)
        ]

    def mark_undone(self, action_ids: list[int]) -> None:
        self.db.executemany(
            "UPDATE actions SET undone = 1 WHERE id = ?", [(i,) for i in action_ids]
        )
        self.db.commit()

    # New posts and uptime

    def is_handled(self, thread_id: int) -> bool:
        query = "SELECT 1 FROM handled_posts WHERE thread_id = ?"
        return self.db.execute(query, (thread_id,)).fetchone() is not None

    def mark_handled(self, thread_id: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO handled_posts VALUES (?, ?)", (thread_id, stamp(now()))
        )
        self.db.commit()

    def get_state(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))
        self.db.commit()

    def purge(self, days: int) -> int:
        """Delete journal entries older than `days`; returns how many rows went."""
        cutoff = stamp(now() - datetime.timedelta(days=days))
        removed = 0
        for sql in (
            "DELETE FROM actions WHERE at < ?",
            "DELETE FROM handled_posts WHERE at < ?",
            "DELETE FROM run_items WHERE run_id IN "
            "(SELECT id FROM runs WHERE started < ? AND status != 'running')",
            "DELETE FROM runs WHERE started < ? AND status != 'running'",
        ):
            removed += self.db.execute(sql, (cutoff,)).rowcount
        self.db.commit()
        return removed

    def close(self) -> None:
        self.db.close()
