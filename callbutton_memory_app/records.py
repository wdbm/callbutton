"""
Identifiers and UTC timestamps shared by the local databases

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import time
import threading
import uuid


@dataclass(frozen=True)
class UtcTimestamp:
    nanoseconds: int
    text: str


_timestamp_lock = threading.Lock()
_last_timestamp_ns = 0


def new_uuid() -> str:
    """
    Return a full UUID version 4 string.
    """
    return str(uuid.uuid4())


def timestamp_from_nanoseconds(nanoseconds: int) -> UtcTimestamp:
    """
    Render Unix nanoseconds as an ISO 8601 UTC timestamp.
    """
    if nanoseconds < 0:
        raise ValueError("a timestamp cannot precede the Unix epoch")
    seconds, fraction = divmod(nanoseconds, 1_000_000_000)
    date = datetime.fromtimestamp(seconds, timezone.utc)
    text = f"{date:%Y-%m-%dT%H:%M:%S}.{fraction:09d}Z"
    return UtcTimestamp(nanoseconds=nanoseconds, text=text)


def utc_now() -> UtcTimestamp:
    """
    Return the current UTC time with nanosecond storage precision.
    """
    global _last_timestamp_ns
    with _timestamp_lock:
        _last_timestamp_ns = max(time.time_ns(), _last_timestamp_ns + 1)
        return timestamp_from_nanoseconds(_last_timestamp_ns)
