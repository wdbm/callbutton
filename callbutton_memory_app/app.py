"""
Program startup, operation and shutdown routines

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

import asyncio
from contextlib import suppress
import os
import signal
import sys

from .audio import Audio, DeviceError, RATE, put_latest, select_device
from .async_utils import BlockingExecutor, run_blocking
from .backend import BackendAgent
from .history_store import HistoryStore, HistoryWriter
from .knowledge_store import KnowledgeStore
from .session import (
    CLOSE_TIMEOUT,
    ContextAppendTracker,
    DelegationManager,
    TranscriptState,
    receive,
    send_microphone,
    send_context_append,
    session_config,
    split_context_append,
    wait_for_start,
)


def create_openai_client(client_class):
    """
    Create the SDK client without negotiating the optional Brotli encoding.
    """
    return client_class(
        api_key=os.environ["OPENAI_API_KEY"],
        default_headers={"Accept-Encoding": "gzip"},
    )


async def _run_session(
    config,
    sound,
    input_device,
    output_device,
    history_store,
    knowledge_store,
    executor,
):
    from openai import AsyncOpenAI

    session_id = await run_blocking(
        executor,
        history_store.start_session,
        config.voice,
        config.backend_model,
        config.timezone,
    )
    writer = HistoryWriter(history_store)
    writer.start()
    closing = asyncio.Event()
    closed = asyncio.Event()
    audio = None
    tasks = []
    signal_installed = False
    close_sent = False
    completed = False
    backend = None
    delegations = None
    append_tracker = ContextAppendTracker()
    error_text = None
    usage = None
    try:
        async with create_openai_client(AsyncOpenAI) as client:
            backend = BackendAgent(
                client,
                config.backend_model,
                config.timezone,
                history_store,
                knowledge_store,
                executor,
                config.web_search,
            )
            async with client.live.connect() as connection:
                events = connection.__aiter__()
                await connection.session.start(
                    session=session_config(config.voice),
                    event_id="callbutton_memory_start",
                )
                started = await wait_for_start(events)
                await run_blocking(
                    executor,
                    history_store.update_session,
                    session_id,
                    "active",
                    started.session.id,
                )
                print(f"Session ready: {started.session.id}", file=sys.stderr)

                summary = await run_blocking(executor, history_store.latest_summary)
                if summary:
                    memory_context = (
                        "Relevant memory from earlier conversations:\n"
                        f"{summary['text'][:4_000]}"
                    )
                    try:
                        for chunk in split_context_append(memory_context):
                            await send_context_append(
                                connection,
                                append_tracker,
                                "thinking",
                                chunk,
                                None,
                            )
                    except Exception as exc:
                        print(
                            f"[session] memory context could not be sent: "
                            f"{type(exc).__name__}: {exc}",
                            file=sys.stderr,
                        )

                audio = Audio(sound, input_device, output_device)
                try:
                    audio.start()
                except Exception:
                    with suppress(Exception):
                        await connection.session.close()
                    raise

                transcript = TranscriptState(session_id, writer, config.transcripts)
                delegations = DelegationManager(
                    connection,
                    session_id,
                    history_store,
                    backend,
                    transcript,
                    append_tracker,
                )
                loop = asyncio.get_running_loop()
                try:
                    loop.add_signal_handler(signal.SIGINT, closing.set)
                    signal_installed = True
                except (NotImplementedError, RuntimeError):
                    pass

                sender = asyncio.create_task(send_microphone(audio, connection, closing))
                receiver = asyncio.create_task(
                    receive(
                        events,
                        audio,
                        transcript,
                        delegations,
                        closed,
                        append_tracker,
                    )
                )

                async def close_session():
                    nonlocal close_sent
                    await closing.wait()
                    audio.stop_input()
                    put_latest(audio.input, b"")
                    try:
                        async with asyncio.timeout(CLOSE_TIMEOUT):
                            await sender
                            await delegations.close()
                            await connection.session.close()
                            close_sent = True
                            await closed.wait()
                    except TimeoutError as exc:
                        with suppress(Exception):
                            await connection.close()
                        raise RuntimeError("timed out waiting for session.closed") from exc

                closer = asyncio.create_task(close_session())
                tasks = [sender, receiver, closer]
                watched = {sender, receiver, closer, audio.failed}
                try:
                    while True:
                        done, _pending = await asyncio.wait(
                            watched, return_when=asyncio.FIRST_COMPLETED
                        )
                        if audio.failed in done:
                            audio.failed.result()
                        if closer in done:
                            closer.result()
                        if receiver in done:
                            usage = receiver.result()
                            print(f"Final session usage: {usage}", file=sys.stderr)
                            completed = True
                            break
                        if sender in done:
                            sender.result()
                            if not closing.is_set():
                                raise RuntimeError("the microphone sender stopped unexpectedly")
                            watched.remove(sender)
                finally:
                    if signal_installed:
                        loop.remove_signal_handler(signal.SIGINT)
                    transcript.flush()
                    audio.stop_input()
                    if delegations is not None:
                        await delegations.close()
                    if not closed.is_set() and not close_sent:
                        with suppress(Exception):
                            await connection.session.close()
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    drained = completed and await audio.drain()
                    audio.close(abort=not drained)
            await writer.flush()
            with suppress(Exception):
                async with asyncio.timeout(20):
                    await backend.update_summary_if_needed()
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        with suppress(Exception):
            await writer.close()
        status = "complete" if completed else "failed"
        await run_blocking(
            executor,
            history_store.update_session,
            session_id,
            status,
            None,
            usage,
            error_text,
        )


async def async_main(config):
    history_store = None
    knowledge_store = None
    executor = BlockingExecutor(max_workers=2, thread_name_prefix="callbutton-memory-db")
    try:
        history_store = HistoryStore(config.history_db)
        knowledge_store = KnowledgeStore(
            config.knowledge_db,
            config.knowledge_dir,
            config.rebuild_knowledge,
            {config.history_db},
        )
        scan = knowledge_store.scan()
        print(
            f"[knowledge] {scan['indexed']} indexed, {scan['removed']} removed, "
            f"{scan['skipped']} skipped at {config.knowledge_dir}",
            file=sys.stderr,
        )
        for warning in scan["warnings"]:
            print(f"[knowledge] skipped {warning}", file=sys.stderr)

        import sounddevice

        devices = sounddevice.query_devices()
        input_device = select_device(config.input_device, "input", devices, sounddevice)
        output_device = select_device(config.output_device, "output", devices, sounddevice)
        print(
            f"[microphone] {input_device} ({devices[input_device]['name']}) at {RATE} Hz",
            file=sys.stderr,
        )
        print(
            f"[speaker] {output_device} ({devices[output_device]['name']}) at {RATE} Hz",
            file=sys.stderr,
        )
        await _run_session(
            config,
            sounddevice,
            input_device,
            output_device,
            history_store,
            knowledge_store,
            executor,
        )
        return 0
    except DeviceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if knowledge_store is not None:
            knowledge_store.close()
        if history_store is not None:
            history_store.close()
        executor.shutdown(wait=True, cancel_futures=True)
