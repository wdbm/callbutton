"""
Helpers for explicitly managed blocking work

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

import asyncio
from concurrent.futures import Executor, Future
import threading


class BlockingExecutor(Executor):
    """
    A small explicitly managed executor for database and file operations
    """

    def __init__(self, max_workers=2, thread_name_prefix="callbutton-memory"):
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.semaphore = threading.Semaphore(max_workers)
        self.closed = False
        self.futures = set()
        self.lock = threading.Lock()
        self.threads = set()
        self.thread_sequence = 0
        self.thread_name_prefix = thread_name_prefix

    def submit(self, function, /, *arguments, **keywords):
        with self.lock:
            if self.closed:
                raise RuntimeError("cannot schedule work after executor shutdown")
            future = Future()
            thread = threading.Thread(
                target=self._invoke,
                args=(future, function, arguments, keywords),
                name=f"{self.thread_name_prefix}-{self.thread_sequence}",
                daemon=True,
            )
            self.thread_sequence += 1
            self.futures.add(future)
            self.threads.add(thread)
            thread.start()
            return future

    def _invoke(self, future, function, arguments, keywords):
        try:
            with self.semaphore:
                if future.set_running_or_notify_cancel():
                    try:
                        result = function(*arguments, **keywords)
                    except BaseException as exc:
                        future.set_exception(exc)
                    else:
                        future.set_result(result)
        finally:
            with self.lock:
                self.futures.discard(future)
                self.threads.discard(threading.current_thread())

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self.lock:
            self.closed = True
            if cancel_futures:
                for future in tuple(self.futures):
                    future.cancel()
            threads = tuple(self.threads)
        if wait:
            for thread in threads:
                thread.join()


async def run_blocking(executor, function, *arguments):
    future = executor.submit(function, *arguments)
    while not future.done():
        await asyncio.sleep(0.001)
    return future.result()
