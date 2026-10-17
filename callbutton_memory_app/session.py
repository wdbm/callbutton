"""
Session handling, transcript reconstruction and client delegation

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

import asyncio
import base64
from contextlib import suppress
from dataclasses import dataclass
import re
import sys
import time

from .audio import RATE
from .async_utils import run_blocking
from .records import new_uuid


MODEL = "gpt-live-1"
START_TIMEOUT = 15
CLOSE_TIMEOUT = 15
MAX_CONTEXT_APPEND_BYTES = 480
APPEND_ACKNOWLEDGEMENT_TYPES = {
    "session.commentary.appended",
    "session.instructions.appended",
    "session.thinking.appended",
}
PROMPT = (
    "You are a personal assistant and private secretary responding to a call button "
    "on an intercom. Respond in English unless another language is requested. Keep "
    "spoken replies brisk. Use brief natural acknowledgements, stop speaking when "
    "interrupted, and listen to the correction."
)
INSTRUCTIONS_SUFFIX = """
Backchannel policy: Use moderate, natural acknowledgements without competing with the caller.
Interruption policy: Stop speaking when the caller interrupts and listen to the correction.
Delegation policy:
- Delegate requests about local knowledge, saved conversation history, current information, web search or careful reasoning to the client backend.
- Delegate before answering when backend work is required, and do not guess while waiting.
- Do not delegate a brief clarification or a request answerable from the current conversation or a still-current result.
- If the caller corrects a request while work is pending, delegate the corrected request; the obsolete work will be cancelled by the client.
""".strip()


def session_config(voice: str):
    return {
        "model": MODEL,
        "instructions": f"{PROMPT}\n\n{INSTRUCTIONS_SUFFIX}",
        "audio": {
            "format": {"type": "audio/pcm", "rate": RATE},
            "output": {"voice": voice},
        },
        "delegation": {"type": "client"},
    }


def split_context_append(content: str, maximum_bytes=MAX_CONTEXT_APPEND_BYTES) -> list[str]:
    """
    Split content conservatively below the configured context-append limit.
    """
    if maximum_bytes < 1:
        raise ValueError("maximum_bytes must be positive")
    remaining = content.strip()
    chunks = []
    while remaining:
        if len(remaining.encode("utf-8")) <= maximum_bytes:
            chunks.append(remaining)
            break

        candidate = remaining[:maximum_bytes]
        while len(candidate.encode("utf-8")) > maximum_bytes:
            candidate = candidate[:-1]
        if not candidate:
            raise ValueError("maximum_bytes is too small for the next character")

        minimum_break = max(1, len(candidate) // 2)
        break_at = None
        for match in re.finditer(r"\n\n|\n|(?<=[.!?])\s+|[;:,]\s+|\s+", candidate):
            if match.end() >= minimum_break:
                break_at = match.end()
        if break_at is None:
            break_at = len(candidate)

        chunk = remaining[:break_at].strip()
        if not chunk:
            break_at = len(candidate)
            chunk = remaining[:break_at]
        chunks.append(chunk)
        remaining = remaining[break_at:].lstrip()
    return chunks


def print_backend_response(content: str) -> None:
    """
    Print a complete backend result distinctly from session transcript fragments.
    """
    print("[backend response]", flush=True)
    print(content, flush=True)
    print("[end backend response]", flush=True)


class ContextAppendTracker:
    """
    Match context acknowledgements and contain append-specific server errors.
    """

    def __init__(self):
        self.pending: dict[str, str] = {}

    def register(self, event_id: str, kind: str) -> None:
        self.pending[event_id] = kind

    def discard(self, event_id: str) -> None:
        self.pending.pop(event_id, None)

    @staticmethod
    def _client_event_id(event):
        client_event_id = getattr(event, "client_event_id", None)
        if client_event_id:
            return client_event_id
        error = getattr(event, "error", None)
        return getattr(error, "client_event_id", None)

    def consume(self, event) -> bool:
        """
        Return true when an error belongs to a tracked context append.
        """
        client_event_id = self._client_event_id(event)
        if event.type in APPEND_ACKNOWLEDGEMENT_TYPES:
            if client_event_id:
                self.discard(client_event_id)
            return False
        if event.type != "error" or client_event_id not in self.pending:
            return False
        kind = self.pending.pop(client_event_id)
        message = getattr(getattr(event, "error", None), "message", "unknown error")
        print(f"[session] {kind} append was rejected: {message}", file=sys.stderr)
        return True


async def send_context_append(connection, tracker, kind, content, delegation_id):
    """
    Send one tracked context append.
    """
    event_id = f"{kind}_{new_uuid()}"
    tracker.register(event_id, kind)
    try:
        channel = getattr(connection.session, kind)
        await channel.append(
            content=content,
            delegation_id=delegation_id,
            event_id=event_id,
        )
    except Exception:
        tracker.discard(event_id)
        raise
    return event_id


@dataclass
class _Turn:
    role: str
    start_ms: int
    end_ms: int
    text: str
    start_fragment_sequence: int
    end_fragment_sequence: int


class TranscriptState:
    """
    Keep immediate transcript state while persistence is queued.
    """

    def __init__(self, session_id, writer, print_transcripts=False):
        self.session_id = session_id
        self.writer = writer
        self.print_transcripts = print_transcripts
        self.fragment_sequence = 0
        self.turn_sequence = 0
        self.current: _Turn | None = None
        self.completed: list[_Turn] = []

    def consume(self, role, event):
        text = event.delta
        if not text:
            return
        if self.print_transcripts:
            print(f"[{role}] {text}", flush=True)
        fragment_sequence = self.fragment_sequence
        self.writer.submit(
            "add_fragment",
            self.session_id,
            self.fragment_sequence,
            role,
            getattr(event, "event_id", None),
            int(event.start_ms),
            int(event.end_ms),
            text,
        )
        self.fragment_sequence += 1
        if self.current is None:
            self.current = _Turn(
                role,
                int(event.start_ms),
                int(event.end_ms),
                text,
                fragment_sequence,
                fragment_sequence,
            )
        elif self.current.role == role:
            self.current.end_ms = max(self.current.end_ms, int(event.end_ms))
            self.current.text += text
            self.current.end_fragment_sequence = fragment_sequence
        else:
            self.flush()
            self.current = _Turn(
                role,
                int(event.start_ms),
                int(event.end_ms),
                text,
                fragment_sequence,
                fragment_sequence,
            )

    def flush(self):
        if self.current is None:
            return
        text = self.current.text.strip()
        turn = self.current
        self.current = None
        if not text:
            return
        turn.text = text
        self.writer.submit(
            "add_turn",
            self.session_id,
            self.turn_sequence,
            turn.role,
            turn.start_ms,
            turn.end_ms,
            turn.text,
            turn.start_fragment_sequence,
            turn.end_fragment_sequence,
            "complete",
        )
        self.turn_sequence += 1
        self.completed.append(turn)
        self.completed = self.completed[-50:]

    def delegated_request(self, offset_ms: int) -> str:
        if self.current is not None and self.current.role == "user":
            request = self.current.text.strip()
            self.flush()
            return request
        for turn in reversed(self.completed):
            if turn.role == "user" and turn.start_ms <= offset_ms:
                return turn.text
        return "The caller requested delegated help; infer the request from the recent transcript."


class DelegationManager:
    def __init__(
        self,
        connection,
        session_id,
        history_store,
        backend,
        transcript,
        append_tracker,
    ):
        self.connection = connection
        self.session_id = session_id
        self.history_store = history_store
        self.backend = backend
        self.transcript = transcript
        self.append_tracker = append_tracker
        self.tasks: dict[str, tuple[asyncio.Task, str]] = {}
        self.semaphore = asyncio.Semaphore(2)
        self.latest_external_id: str | None = None

    def start(self, event):
        for task, record_id in list(self.tasks.values()):
            if record_id and not task.done():
                task.cancel()
        request_text = self.transcript.delegated_request(int(event.offset_ms))
        self.latest_external_id = event.delegation.id
        task = asyncio.create_task(self._handle(event, request_text))
        self.tasks[event.delegation.id] = (task, "")
        task.add_done_callback(lambda _task, key=event.delegation.id: self._finished(key))

    def _finished(self, external_id):
        entry = self.tasks.pop(external_id, None)
        if entry is not None and entry[0].done():
            with suppress(asyncio.CancelledError, Exception):
                entry[0].result()

    async def _handle(self, event, request_text):
        started = time.monotonic_ns()
        record_id = None
        try:
            record_id = await run_blocking(
                self.backend.executor,
                self.history_store.create_delegation,
                self.session_id,
                event.delegation.id,
                int(event.offset_ms),
                request_text,
            )
            task = asyncio.current_task()
            if task is not None:
                self.tasks[event.delegation.id] = (task, record_id)
            if event.delegation.id != self.latest_external_id:
                await run_blocking(
                    self.backend.executor,
                    self.history_store.finish_delegation,
                    record_id,
                    "superseded",
                    None,
                    None,
                    (time.monotonic_ns() - started) // 1_000_000,
                )
                return
            async with self.semaphore:
                result = await self.backend.answer(record_id, request_text)
            latency_ms = (time.monotonic_ns() - started) // 1_000_000
            await run_blocking(
                self.backend.executor,
                self.history_store.finish_delegation,
                record_id,
                "complete",
                result,
                None,
                latency_ms,
            )
            chunks = split_context_append(result)
            if len(chunks) > 1:
                print(
                    f"[backend] complete response requires {len(chunks)} safe context appends; "
                    "the full text was saved to conversation history",
                    file=sys.stderr,
                )
                if self.transcript.print_transcripts:
                    print_backend_response(result)
            try:
                for chunk in chunks:
                    await send_context_append(
                        self.connection,
                        self.append_tracker,
                        "commentary",
                        chunk,
                        event.delegation.id,
                    )
            except Exception as exc:
                print(
                    f"[session] commentary append could not be sent: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
        except asyncio.CancelledError:
            latency_ms = (time.monotonic_ns() - started) // 1_000_000
            if record_id is not None:
                await run_blocking(
                    self.backend.executor,
                    self.history_store.finish_delegation,
                    record_id,
                    "superseded",
                    None,
                    None,
                    latency_ms,
                )
            raise
        except Exception as exc:
            latency_ms = (time.monotonic_ns() - started) // 1_000_000
            if record_id is not None:
                await run_blocking(
                    self.backend.executor,
                    self.history_store.finish_delegation,
                    record_id,
                    "failed",
                    None,
                    str(exc),
                    latency_ms,
                )
            print(f"[backend] {type(exc).__name__}: {exc}", file=sys.stderr)
            with suppress(Exception):
                await send_context_append(
                    self.connection,
                    self.append_tracker,
                    "commentary",
                    "The requested lookup failed. Please tell the caller briefly and offer to try again.",
                    event.delegation.id,
                )

    async def close(self):
        pending = [task for task, _record_id in self.tasks.values() if not task.done()]
        if pending:
            done, still_pending = await asyncio.wait(pending, timeout=10)
            for task in still_pending:
                task.cancel()
            if still_pending:
                await asyncio.gather(*still_pending, return_exceptions=True)
            for task in done:
                with suppress(asyncio.CancelledError, Exception):
                    task.result()


async def wait_for_start(events):
    try:
        async with asyncio.timeout(START_TIMEOUT):
            while True:
                event = await anext(events)
                if event.type == "error":
                    raise RuntimeError(event.error.message)
                if event.type == "session.started":
                    return event
    except StopAsyncIteration as exc:
        raise RuntimeError("the connection closed before session.started") from exc
    except TimeoutError as exc:
        raise RuntimeError("timed out waiting for session.started") from exc


async def send_microphone(audio, connection, closing):
    pending = b""
    while not closing.is_set():
        chunk = pending + await audio.input.get()
        if closing.is_set():
            return
        complete = len(chunk) - len(chunk) % 2
        pending = chunk[complete:]
        if complete:
            encoded = base64.b64encode(chunk[:complete]).decode("ascii")
            await connection.session.input_audio.append(audio=encoded)


async def receive(events, audio, transcript, delegations, closed, append_tracker=None):
    try:
        while True:
            event = await anext(events)
            if event.type == "error":
                if append_tracker is not None and append_tracker.consume(event):
                    continue
                raise RuntimeError(event.error.message)
            if append_tracker is not None:
                append_tracker.consume(event)
            if event.type == "session.output_audio.delta":
                pcm = base64.b64decode(event.delta, validate=True)
                if len(pcm) % 2:
                    raise RuntimeError("an incomplete PCM16 output sample was received")
                audio.play(pcm)
            elif event.type in {
                "session.input_transcript.delta",
                "session.output_transcript.delta",
            }:
                role = "user" if event.type.startswith("session.input") else "assistant"
                transcript.consume(role, event)
            elif event.type == "session.delegation.created":
                delegations.start(event)
            elif event.type == "session.closed":
                transcript.flush()
                closed.set()
                return event.usage
    except StopAsyncIteration as exc:
        raise RuntimeError("the connection closed without session.closed") from exc
