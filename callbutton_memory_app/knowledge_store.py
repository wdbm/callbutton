"""
Disposable FTS5 index for a read-only local knowledge directory

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading

from .records import new_uuid, utc_now


SCHEMA_VERSION = 1
MAX_FILE_BYTES = 5 * 1024 * 1024
CHUNK_TARGET = 4_000
CHUNK_OVERLAP = 400
EXCLUDED_DIRECTORIES = {"__pycache__", "node_modules", "venv", ".venv"}
EXCLUDED_SUFFIXES = {".key", ".p12", ".pfx", ".pem"}
EXCLUDED_NAMES = {"credentials", "credentials.json", "id_rsa", "id_ed25519"}


def _fts_query(query: str) -> str:
    tokens = re.findall(r"[^\W_]+(?:['-][^\W_]+)*", query, flags=re.UNICODE)
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:20])


def _chunks(text: str):
    """
    Yield bounded, line-addressable chunks with modest overlap.
    """
    lines = text.splitlines(keepends=True)
    if not lines and text:
        lines = [text]
    start = 0
    chunk_number = 0
    while start < len(lines):
        end = start
        size = 0
        last_boundary = None
        while end < len(lines) and (size < CHUNK_TARGET or end == start):
            line = lines[end]
            if line.strip() == "" or line.lstrip().startswith("#"):
                last_boundary = end + 1
            size += len(line)
            end += 1
            if size >= CHUNK_TARGET:
                break
        if end < len(lines) and last_boundary is not None and last_boundary > start:
            end = last_boundary
        content = "".join(lines[start:end]).strip()
        if content:
            yield chunk_number, start + 1, end, content
            chunk_number += 1
        if end >= len(lines):
            break
        overlap_size = 0
        next_start = end
        while next_start > start and overlap_size < CHUNK_OVERLAP:
            next_start -= 1
            overlap_size += len(lines[next_start])
        start = max(start + 1, next_start)


class KnowledgeStore:
    def __init__(self, path: Path, root: Path, rebuild: bool = False, excluded_paths=()):
        self.path = Path(path)
        self.root = Path(root).resolve(strict=False)
        base_exclusions = {
            Path(item).resolve(strict=False) for item in (set(excluded_paths) | {self.path})
        }
        self.excluded_paths = set(base_exclusions)
        for item in base_exclusions:
            self.excluded_paths.add(Path(f"{item}-wal").resolve(strict=False))
            self.excluded_paths.add(Path(f"{item}-shm").resolve(strict=False))
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if rebuild:
            with suppress(FileNotFoundError):
                self.path.unlink()
            with suppress(FileNotFoundError):
                Path(f"{self.path}-wal").unlink()
            with suppress(FileNotFoundError):
                Path(f"{self.path}-shm").unlink()
        created = not self.path.exists()
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        try:
            self._configure()
            self._create_schema()
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
                    schema_version INTEGER NOT NULL,
                    knowledge_root TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS index_runs (
                    index_run_id TEXT PRIMARY KEY,
                    created_at_ns INTEGER NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    status TEXT NOT NULL,
                    indexed_count INTEGER NOT NULL,
                    skipped_count INTEGER NOT NULL,
                    removed_count INTEGER NOT NULL,
                    warnings_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    created_at_ns INTEGER NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    relative_path TEXT NOT NULL UNIQUE,
                    size_bytes INTEGER NOT NULL,
                    modified_at_ns INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    line_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    created_at_ns INTEGER NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                    chunk_number INTEGER NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    UNIQUE(document_id, chunk_number)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    relative_path,
                    content,
                    tokenize='unicode61'
                );
                CREATE INDEX IF NOT EXISTS chunks_document
                    ON chunks(document_id, chunk_number);
                """
            )
            row = self.connection.execute(
                "SELECT schema_version, knowledge_root FROM database_metadata LIMIT 1"
            ).fetchone()
            user_version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if row is None:
                now = utc_now()
                self.connection.execute(
                    "INSERT INTO database_metadata VALUES (?, ?, ?, ?, ?)",
                    (new_uuid(), now.nanoseconds, now.text, SCHEMA_VERSION, str(self.root)),
                )
                self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif (
                row["schema_version"] != SCHEMA_VERSION
                or user_version != SCHEMA_VERSION
            ):
                raise RuntimeError("the knowledge database format is not supported")
            elif Path(row["knowledge_root"]) != self.root:
                raise RuntimeError(
                    "the knowledge database belongs to a different knowledge directory; "
                    "select another database or use --rebuild-knowledge"
                )

    def _eligible_files(self):
        for directory, names, filenames in os.walk(self.root, followlinks=False):
            directory_path = Path(directory)
            names[:] = sorted(
                name
                for name in names
                if not name.startswith(".")
                and name not in EXCLUDED_DIRECTORIES
                and not (directory_path / name).is_symlink()
            )
            for name in sorted(filenames):
                path = directory_path / name
                if name.startswith(".") or path.is_symlink():
                    continue
                if name.casefold() in EXCLUDED_NAMES or path.suffix.casefold() in EXCLUDED_SUFFIXES:
                    continue
                resolved = path.resolve(strict=False)
                if resolved in self.excluded_paths:
                    continue
                yield path

    def _read_text(self, path: Path):
        before = path.stat()
        if before.st_size > MAX_FILE_BYTES:
            raise ValueError("file exceeds the 5 MiB limit")
        raw = path.read_bytes()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("file changed while it was being read")
        if b"\x00" in raw:
            raise ValueError("file contains NUL bytes")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("file is not valid UTF-8 text") from exc
        return before, raw, text

    def scan(self) -> dict:
        run_id = new_uuid()
        now = utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO index_runs VALUES (?, ?, ?, ?, ?, 'running', 0, 0, 0, '[]')",
                (run_id, now.nanoseconds, now.text, now.nanoseconds, now.text),
            )
        indexed = 0
        skipped = 0
        removed = 0
        warnings = []
        seen = set()
        try:
            for path in self._eligible_files():
                relative_path = path.relative_to(self.root).as_posix()
                seen.add(relative_path)
                existing = None
                try:
                    stat = path.stat()
                    with self.lock:
                        existing = self.connection.execute(
                            """
                            SELECT document_id, size_bytes, modified_at_ns
                            FROM documents WHERE relative_path = ?
                            """,
                            (relative_path,),
                        ).fetchone()
                    if (
                        existing is not None
                        and existing["size_bytes"] == stat.st_size
                        and existing["modified_at_ns"] == stat.st_mtime_ns
                    ):
                        continue
                    stable_stat, raw, text = self._read_text(path)
                    digest = hashlib.sha256(raw).hexdigest()
                    self._replace_document(relative_path, stable_stat, digest, text, existing)
                    indexed += 1
                except (OSError, ValueError) as exc:
                    skipped += 1
                    warnings.append(f"{relative_path}: {exc}")
                    seen.discard(relative_path)
                    if existing is not None:
                        with self.lock, self.connection:
                            self._delete_document(relative_path)
                        removed += 1

            with self.lock, self.connection:
                existing_paths = {
                    row["relative_path"]
                    for row in self.connection.execute("SELECT relative_path FROM documents")
                }
                for relative_path in existing_paths - seen:
                    self._delete_document(relative_path)
                    removed += 1
                finished = utc_now()
                self.connection.execute(
                    """
                    UPDATE index_runs SET updated_at_ns = ?, updated_at_utc = ?, status = 'complete',
                        indexed_count = ?, skipped_count = ?, removed_count = ?, warnings_json = ?
                    WHERE index_run_id = ?
                    """,
                    (
                        finished.nanoseconds,
                        finished.text,
                        indexed,
                        skipped,
                        removed,
                        json.dumps(warnings, ensure_ascii=False),
                        run_id,
                    ),
                )
        except Exception as exc:
            failed = utc_now()
            with self.lock, self.connection:
                self.connection.execute(
                    """
                    UPDATE index_runs SET updated_at_ns = ?, updated_at_utc = ?, status = 'failed',
                        warnings_json = ? WHERE index_run_id = ?
                    """,
                    (failed.nanoseconds, failed.text, json.dumps([str(exc)]), run_id),
                )
            raise
        return {
            "index_run_id": run_id,
            "indexed": indexed,
            "skipped": skipped,
            "removed": removed,
            "warnings": warnings,
        }

    def _delete_document(self, relative_path: str):
        rows = self.connection.execute(
            """
            SELECT c.chunk_id FROM chunks AS c
            JOIN documents AS d ON d.document_id = c.document_id
            WHERE d.relative_path = ?
            """,
            (relative_path,),
        ).fetchall()
        for row in rows:
            self.connection.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (row["chunk_id"],))
        self.connection.execute("DELETE FROM documents WHERE relative_path = ?", (relative_path,))

    def _replace_document(self, relative_path, stat, digest, text, existing):
        now = utc_now()
        line_count = len(text.splitlines())
        with self.lock, self.connection:
            if existing is None:
                document_id = new_uuid()
                self.connection.execute(
                    "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        document_id,
                        now.nanoseconds,
                        now.text,
                        now.nanoseconds,
                        now.text,
                        relative_path,
                        stat.st_size,
                        stat.st_mtime_ns,
                        digest,
                        line_count,
                    ),
                )
            else:
                document_id = existing["document_id"]
                old_chunks = self.connection.execute(
                    "SELECT chunk_id FROM chunks WHERE document_id = ?", (document_id,)
                ).fetchall()
                for old_chunk in old_chunks:
                    self.connection.execute(
                        "DELETE FROM chunks_fts WHERE chunk_id = ?", (old_chunk["chunk_id"],)
                    )
                self.connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
                self.connection.execute(
                    """
                    UPDATE documents SET updated_at_ns = ?, updated_at_utc = ?, size_bytes = ?,
                        modified_at_ns = ?, content_hash = ?, line_count = ? WHERE document_id = ?
                    """,
                    (
                        now.nanoseconds,
                        now.text,
                        stat.st_size,
                        stat.st_mtime_ns,
                        digest,
                        line_count,
                        document_id,
                    ),
                )
            for chunk_number, start_line, end_line, content in _chunks(text):
                chunk_id = new_uuid()
                chunk_time = utc_now()
                self.connection.execute(
                    "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk_id,
                        chunk_time.nanoseconds,
                        chunk_time.text,
                        chunk_time.nanoseconds,
                        chunk_time.text,
                        document_id,
                        chunk_number,
                        start_line,
                        end_line,
                        content,
                    ),
                )
                self.connection.execute(
                    "INSERT INTO chunks_fts(chunk_id, relative_path, content) VALUES (?, ?, ?)",
                    (chunk_id, relative_path, content),
                )

    def search(self, query: str, limit: int = 5) -> list[dict]:
        match = _fts_query(query)
        if not match:
            return []
        limit = max(1, min(int(limit), 10))
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT c.chunk_id, d.relative_path AS path, c.start_line, c.end_line,
                       c.content, bm25(chunks_fts) AS score
                FROM chunks_fts
                JOIN chunks AS c ON c.chunk_id = chunks_fts.chunk_id
                JOIN documents AS d ON d.document_id = c.document_id
                WHERE chunks_fts MATCH ?
                ORDER BY score LIMIT ?
                """,
                (match, limit * 3),
            ).fetchall()
        results = []
        seen_paths = set()
        for row in rows:
            item = dict(row)
            if item["path"] in seen_paths:
                continue
            seen_paths.add(item["path"])
            item["content"] = item["content"][:2_000]
            results.append(item)
            if len(results) >= limit:
                break
        return results

    def _safe_path(self, relative_path: str) -> Path:
        supplied = Path(relative_path)
        if supplied.is_absolute() or any(part.startswith(".") for part in supplied.parts):
            raise ValueError("only non-hidden paths relative to the knowledge directory are allowed")
        if any(part in EXCLUDED_DIRECTORIES for part in supplied.parts[:-1]):
            raise ValueError("the requested path belongs to an excluded directory")
        if (
            supplied.name.casefold() in EXCLUDED_NAMES
            or supplied.suffix.casefold() in EXCLUDED_SUFFIXES
        ):
            raise ValueError("the requested file type is excluded from knowledge")
        source = self.root / supplied
        current = self.root
        for part in supplied.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("symbolic links are not eligible knowledge paths")
        candidate = source.resolve(strict=True)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("the requested path leaves the knowledge directory") from exc
        if not candidate.is_file():
            raise ValueError("the requested path is not an eligible regular file")
        if candidate.resolve() in self.excluded_paths:
            raise ValueError("the requested path is excluded from knowledge")
        return candidate

    def read_excerpt(self, relative_path: str, start_line: int, end_line: int) -> dict:
        path = self._safe_path(relative_path)
        _, _, text = self._read_text(path)
        lines = text.splitlines()
        start_line = max(1, int(start_line))
        end_line = min(len(lines), int(end_line), start_line + 199)
        if end_line < start_line:
            raise ValueError("end_line must not precede start_line")
        return {
            "path": path.relative_to(self.root).as_posix(),
            "start_line": start_line,
            "end_line": end_line,
            "content": "\n".join(lines[start_line - 1:end_line])[:12_000],
        }

    def close(self):
        with self.lock:
            self.connection.close()
