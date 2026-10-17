"""
Durable conversation history stored separately from local knowledge

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

import asyncio
from contextlib import suppress
import json
from pathlib import Path
import queue
import re
import sqlite3
import threading
from typing import Any

from .records import new_uuid, utc_now


SCHEMA_VERSION = 2


def _fts_query(query: str) -> str:
    tokens = re.findall(r"[^\W_]+(?:['-][^\W_]+)*", query, flags=re.UNICODE)
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:20])


class HistoryStore:
    """
    Own the durable history connection and its serialised operations.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        created = not self.path.exists()
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        try:
            self._configure()
            self._create_schema()
            self._recover_interrupted_sessions()
        except BaseException:
            self.connection.close()
            raise
        if created:
            with suppress(OSError):
                self.path.chmod(0o600)

    def _configure(self):
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")

    def _create_schema(self):
        with self.lock, self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS database_metadata (
                    metadata_id TEXT PRIMARY KEY,
                    created_at_ns INTEGER NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    schema_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at_ns INTEGER NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    provider_session_id TEXT,
                    status TEXT NOT NULL,
                    voice TEXT NOT NULL,
                    backend_model TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    usage_json TEXT,
                    error_text TEXT
                );
                CREATE TABLE IF NOT EXISTS transcript_fragments (
                    fragment_id TEXT PRIMARY KEY,
                    created_at_ns INTEGER NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    sequence_number INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    event_id TEXT,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    UNIQUE(session_id, sequence_number)
                );
                CREATE TABLE IF NOT EXISTS turns (
                    turn_id TEXT PRIMARY KEY,
                    created_at_ns INTEGER NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    sequence_number INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    start_fragment_sequence INTEGER NOT NULL,
                    end_fragment_sequence INTEGER NOT NULL,
                    completion_status TEXT NOT NULL,
                    text TEXT NOT NULL,
                    UNIQUE(session_id, sequence_number)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
                    turn_id UNINDEXED,
                    text,
                    tokenize='unicode61'
                );
                CREATE TABLE IF NOT EXISTS delegations (
                    delegation_id TEXT PRIMARY KEY,
                    created_at_ns INTEGER NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    external_delegation_id TEXT NOT NULL,
                    offset_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    request_text TEXT NOT NULL,
                    response_text TEXT,
                    error_text TEXT,
                    latency_ms INTEGER,
                    UNIQUE(session_id, external_delegation_id)
                );
                CREATE TABLE IF NOT EXISTS tool_runs (
                    tool_run_id TEXT PRIMARY KEY,
                    created_at_ns INTEGER NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id) ON DELETE CASCADE,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    result_json TEXT,
                    status TEXT NOT NULL,
                    error_text TEXT
                );
                CREATE TABLE IF NOT EXISTS memory_summaries (
                    summary_id TEXT PRIMARY KEY,
                    created_at_ns INTEGER NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    through_turn_ns INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    source_turn_count INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS transcript_session_sequence
                    ON transcript_fragments(session_id, sequence_number);
                CREATE INDEX IF NOT EXISTS turns_session_sequence
                    ON turns(session_id, sequence_number);
                CREATE INDEX IF NOT EXISTS delegations_session
                    ON delegations(session_id, created_at_ns);
                """
            )
            row = self.connection.execute(
                "SELECT schema_version FROM database_metadata LIMIT 1"
            ).fetchone()
            user_version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if row is None:
                record_id = new_uuid()
                now = utc_now()
                self.connection.execute(
                    "INSERT INTO database_metadata VALUES (?, ?, ?, ?)",
                    (record_id, now.nanoseconds, now.text, SCHEMA_VERSION),
                )
                self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif (
                row["schema_version"] != SCHEMA_VERSION
                or user_version != SCHEMA_VERSION
            ):
                raise RuntimeError("the history database format is not supported")

    def _recover_interrupted_sessions(self):
        """
        Rebuild turns from lossless fragments after an abrupt exit.
        """
        with self.lock, self.connection:
            sessions = self.connection.execute(
                "SELECT session_id FROM sessions WHERE status IN ('starting', 'active')"
            ).fetchall()
            for session in sessions:
                session_id = session["session_id"]
                old_turns = self.connection.execute(
                    "SELECT turn_id FROM turns WHERE session_id = ?", (session_id,)
                ).fetchall()
                for turn in old_turns:
                    self.connection.execute(
                        "DELETE FROM turns_fts WHERE turn_id = ?", (turn["turn_id"],)
                    )
                self.connection.execute("DELETE FROM turns WHERE session_id = ?", (session_id,))
                fragments = self.connection.execute(
                    """
                    SELECT sequence_number, role, start_ms, end_ms, text
                    FROM transcript_fragments
                    WHERE session_id = ? ORDER BY sequence_number
                    """,
                    (session_id,),
                ).fetchall()
                assembled = []
                for fragment in fragments:
                    if assembled and assembled[-1][0] == fragment["role"]:
                        assembled[-1][2] = max(assembled[-1][2], fragment["end_ms"])
                        assembled[-1][3] += fragment["text"]
                        assembled[-1][5] = fragment["sequence_number"]
                    else:
                        assembled.append(
                            [
                                fragment["role"],
                                fragment["start_ms"],
                                fragment["end_ms"],
                                fragment["text"],
                                fragment["sequence_number"],
                                fragment["sequence_number"],
                            ]
                        )
                for sequence_number, (
                    role,
                    start_ms,
                    end_ms,
                    text,
                    start_fragment_sequence,
                    end_fragment_sequence,
                ) in enumerate(assembled):
                    clean_text = text.strip()
                    if not clean_text:
                        continue
                    turn_id = new_uuid()
                    now = utc_now()
                    self.connection.execute(
                        "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            turn_id,
                            now.nanoseconds,
                            now.text,
                            session_id,
                            sequence_number,
                            role,
                            start_ms,
                            end_ms,
                            start_fragment_sequence,
                            end_fragment_sequence,
                            "recovered",
                            clean_text,
                        ),
                    )
                    self.connection.execute(
                        "INSERT INTO turns_fts(turn_id, text) VALUES (?, ?)",
                        (turn_id, clean_text),
                    )
                now = utc_now()
                self.connection.execute(
                    """
                    UPDATE sessions SET updated_at_ns = ?, updated_at_utc = ?,
                        status = 'interrupted',
                        error_text = COALESCE(error_text, 'process ended before clean shutdown')
                    WHERE session_id = ?
                    """,
                    (now.nanoseconds, now.text, session_id),
                )

    def start_session(self, voice: str, backend_model: str, timezone: str) -> str:
        session_id = new_uuid()
        now = utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO sessions (
                    session_id, created_at_ns, created_at_utc, updated_at_ns,
                    updated_at_utc, status, voice, backend_model, timezone
                ) VALUES (?, ?, ?, ?, ?, 'starting', ?, ?, ?)
                """,
                (
                    session_id,
                    now.nanoseconds,
                    now.text,
                    now.nanoseconds,
                    now.text,
                    voice,
                    backend_model,
                    timezone,
                ),
            )
        return session_id

    def update_session(
        self,
        session_id: str,
        status: str,
        provider_session_id: str | None = None,
        usage: Any = None,
        error_text: str | None = None,
    ) -> None:
        now = utc_now()
        usage_json = None if usage is None else json.dumps(usage, default=str, ensure_ascii=False)
        with self.lock, self.connection:
            self.connection.execute(
                """
                UPDATE sessions SET updated_at_ns = ?, updated_at_utc = ?, status = ?,
                    provider_session_id = COALESCE(?, provider_session_id),
                    usage_json = COALESCE(?, usage_json),
                    error_text = COALESCE(?, error_text)
                WHERE session_id = ?
                """,
                (
                    now.nanoseconds,
                    now.text,
                    status,
                    provider_session_id,
                    usage_json,
                    error_text,
                    session_id,
                ),
            )

    def add_fragment(
        self,
        session_id: str,
        sequence_number: int,
        role: str,
        event_id: str | None,
        start_ms: int,
        end_ms: int,
        text: str,
    ) -> str:
        fragment_id = new_uuid()
        now = utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO transcript_fragments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fragment_id,
                    now.nanoseconds,
                    now.text,
                    session_id,
                    sequence_number,
                    role,
                    event_id,
                    start_ms,
                    end_ms,
                    text,
                ),
            )
        return fragment_id

    def add_turn(
        self,
        session_id: str,
        sequence_number: int,
        role: str,
        start_ms: int,
        end_ms: int,
        text: str,
        start_fragment_sequence: int = -1,
        end_fragment_sequence: int = -1,
        completion_status: str = "complete",
    ) -> str:
        turn_id = new_uuid()
        now = utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    now.nanoseconds,
                    now.text,
                    session_id,
                    sequence_number,
                    role,
                    start_ms,
                    end_ms,
                    start_fragment_sequence,
                    end_fragment_sequence,
                    completion_status,
                    text,
                ),
            )
            self.connection.execute(
                "INSERT INTO turns_fts(turn_id, text) VALUES (?, ?)",
                (turn_id, text),
            )
        return turn_id

    def recent_turns(self, limit: int = 24) -> list[dict]:
        limit = max(1, min(int(limit), 100))
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT turn_id, session_id, created_at_utc, role, text
                FROM turns ORDER BY created_at_ns DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def search_turns(self, query: str, limit: int = 5) -> list[dict]:
        match = _fts_query(query)
        if not match:
            return []
        limit = max(1, min(int(limit), 20))
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT t.turn_id, t.session_id, t.created_at_utc, t.role, t.text,
                       bm25(turns_fts) AS score
                FROM turns_fts
                JOIN turns AS t ON t.turn_id = turns_fts.turn_id
                WHERE turns_fts MATCH ?
                ORDER BY score, t.created_at_ns DESC LIMIT ?
                """,
                (match, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_delegation(
        self,
        session_id: str,
        external_delegation_id: str,
        offset_ms: int,
        request_text: str,
    ) -> str:
        delegation_id = new_uuid()
        now = utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO delegations VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, NULL, NULL, NULL
                )
                """,
                (
                    delegation_id,
                    now.nanoseconds,
                    now.text,
                    now.nanoseconds,
                    now.text,
                    session_id,
                    external_delegation_id,
                    offset_ms,
                    request_text,
                ),
            )
        return delegation_id

    def finish_delegation(
        self,
        delegation_id: str,
        status: str,
        response_text: str | None = None,
        error_text: str | None = None,
        latency_ms: int | None = None,
    ) -> None:
        now = utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                """
                UPDATE delegations SET updated_at_ns = ?, updated_at_utc = ?, status = ?,
                    response_text = ?, error_text = ?, latency_ms = ?
                WHERE delegation_id = ?
                """,
                (
                    now.nanoseconds,
                    now.text,
                    status,
                    response_text,
                    error_text,
                    latency_ms,
                    delegation_id,
                ),
            )

    def start_tool_run(self, delegation_id: str, tool_name: str, arguments: dict) -> str:
        tool_run_id = new_uuid()
        now = utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO tool_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'running', NULL)
                """,
                (
                    tool_run_id,
                    now.nanoseconds,
                    now.text,
                    now.nanoseconds,
                    now.text,
                    delegation_id,
                    tool_name,
                    json.dumps(arguments, ensure_ascii=False, sort_keys=True),
                ),
            )
        return tool_run_id

    def finish_tool_run(
        self,
        tool_run_id: str,
        status: str,
        result: Any = None,
        error_text: str | None = None,
    ) -> None:
        now = utc_now()
        result_json = None if result is None else json.dumps(result, ensure_ascii=False)
        with self.lock, self.connection:
            self.connection.execute(
                """
                UPDATE tool_runs SET updated_at_ns = ?, updated_at_utc = ?, status = ?,
                    result_json = ?, error_text = ? WHERE tool_run_id = ?
                """,
                (now.nanoseconds, now.text, status, result_json, error_text, tool_run_id),
            )

    def add_summary(self, through_turn_ns: int, text: str, source_turn_count: int) -> str:
        summary_id = new_uuid()
        now = utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO memory_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    summary_id,
                    now.nanoseconds,
                    now.text,
                    now.nanoseconds,
                    now.text,
                    through_turn_ns,
                    text,
                    source_turn_count,
                ),
            )
        return summary_id

    def latest_summary(self) -> dict | None:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT summary_id, created_at_utc, through_turn_ns, text, source_turn_count
                FROM memory_summaries ORDER BY through_turn_ns DESC LIMIT 1
                """
            ).fetchone()
        return None if row is None else dict(row)

    def count_turns_after(self, timestamp_ns: int) -> int:
        with self.lock:
            row = self.connection.execute(
                "SELECT COUNT(*) AS count FROM turns WHERE created_at_ns > ?",
                (timestamp_ns,),
            ).fetchone()
        return int(row["count"])

    def latest_turn_timestamp(self) -> int:
        with self.lock:
            row = self.connection.execute(
                "SELECT COALESCE(MAX(created_at_ns), 0) AS value FROM turns"
            ).fetchone()
        return int(row["value"])

    def close(self):
        with self.lock:
            self.connection.close()


class HistoryWriter:
    """
    Serialise small history writes away from the session receive loop.
    """

    def __init__(self, store: HistoryStore):
        self.store = store
        self.queue: queue.Queue = queue.Queue(maxsize=4_096)
        self.thread: threading.Thread | None = None
        self.failure: BaseException | None = None

    def start(self):
        self.thread = threading.Thread(
            target=self._run,
            name="callbutton-memory-history",
            daemon=True,
        )
        self.thread.start()

    def submit(self, method_name: str, *arguments):
        if self.failure is not None:
            raise RuntimeError("the history writer has failed") from self.failure
        self.queue.put_nowait((method_name, arguments))

    def _run(self):
        try:
            while True:
                item = self.queue.get()
                if item is None:
                    self.queue.task_done()
                    return
                method_name, arguments = item
                try:
                    getattr(self.store, method_name)(*arguments)
                finally:
                    self.queue.task_done()
        except BaseException as exc:
            self.failure = exc
            while not self.queue.empty():
                with suppress(queue.Empty):
                    self.queue.get_nowait()
                    self.queue.task_done()

    async def flush(self):
        while self.queue.unfinished_tasks:
            await asyncio.sleep(0.005)
        if self.failure is not None:
            raise RuntimeError("the history writer failed") from self.failure

    async def close(self):
        if self.thread is None:
            return
        await self.flush()
        self.queue.put_nowait(None)
        while self.thread.is_alive():
            await asyncio.sleep(0.005)
        self.thread.join()
