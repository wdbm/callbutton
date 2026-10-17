"""
Bounded full-duplex PCM16 audio transport

Copyright (C) 2026 William Breaden Madden
SPDX-License-Identifier: GPL-3.0-or-later
"""

import asyncio
from contextlib import suppress
import queue
import sys
import threading


RATE = 24_000
CHUNK_MS = 40
CHUNK_BYTES = RATE * 2 * CHUNK_MS // 1000
INPUT_BLOCKS = 8
OUTPUT_BLOCKS = 7


class DeviceError(ValueError):
    pass


def device_supports(device, kind):
    field = "max_input_channels" if kind == "input" else "max_output_channels"
    return int(device.get(field, 0)) > 0


def select_device(specification, kind, devices, sound):
    if specification is None:
        try:
            index = int(sound.default.device[0 if kind == "input" else 1])
        except (AttributeError, IndexError, TypeError, ValueError):
            index = -1
        if index < 0 or index >= len(devices) or not device_supports(devices[index], kind):
            raise DeviceError(
                f"the system-default {kind} sound device is unavailable; supply --{kind}-device"
            )
    else:
        try:
            index = int(specification)
        except ValueError:
            wanted = specification.casefold()
            candidates = [
                (number, device)
                for number, device in enumerate(devices)
                if device_supports(device, kind)
            ]
            exact = [
                number
                for number, device in candidates
                if str(device.get("name", "")).casefold() == wanted
            ]
            partial = [
                number
                for number, device in candidates
                if wanted in str(device.get("name", "")).casefold()
            ]
            matches = exact or partial
            if not matches:
                raise DeviceError(f"no {kind} sound device matches {specification!r}")
            index = matches[0]
        if index < 0 or index >= len(devices) or not device_supports(devices[index], kind):
            raise DeviceError(f"sound device {index} is unavailable or does not support {kind}")

    check = sound.check_input_settings if kind == "input" else sound.check_output_settings
    try:
        check(device=index, channels=1, dtype="int16", samplerate=RATE)
    except Exception as exc:
        name = devices[index].get("name", "unnamed")
        raise DeviceError(
            f"{kind} device {index} ({name}) does not support mono PCM16 at {RATE} Hz: {exc}"
        ) from exc
    return index


def put_latest(buffer, value):
    try:
        buffer.put_nowait(value)
        return False
    except (asyncio.QueueFull, queue.Full):
        with suppress(asyncio.QueueEmpty, queue.Empty):
            buffer.get_nowait()
        buffer.put_nowait(value)
        return True


class Audio:
    def __init__(self, sound, input_device, output_device):
        self.sound = sound
        self.input_device = input_device
        self.output_device = output_device
        self.loop = asyncio.get_running_loop()
        self.input = asyncio.Queue(maxsize=INPUT_BLOCKS)
        self.output = queue.Queue(maxsize=OUTPUT_BLOCKS)
        self.failed = self.loop.create_future()
        self.mic = None
        self.speaker = None
        self.speaker_thread = None
        self.input_stopping = threading.Event()
        self.stopping = threading.Event()
        self.writing = threading.Event()
        self.input_drop_reported = False
        self.output_drop_reported = False
        self.input_status_reported = False
        self.output_underflow_reported = False

    def fail(self, error):
        if not self.failed.done():
            self.failed.set_exception(error)

    def accept_input(self, raw, status):
        if self.input_stopping.is_set():
            return
        if put_latest(self.input, raw) and not self.input_drop_reported:
            self.input_drop_reported = True
            print("[audio] microphone backlog was trimmed", file=sys.stderr)
        if status and not self.input_status_reported:
            self.input_status_reported = True
            print(f"[audio] microphone status: {status}", file=sys.stderr)

    def input_callback(self, data, _frames, _time, status):
        self.loop.call_soon_threadsafe(self.accept_input, bytes(data), status)

    def input_finished(self):
        if not self.input_stopping.is_set():
            self.loop.call_soon_threadsafe(
                self.fail, RuntimeError("the microphone stream stopped unexpectedly")
            )

    def play(self, pcm):
        for offset in range(0, len(pcm), CHUNK_BYTES):
            dropped = put_latest(self.output, pcm[offset:offset + CHUNK_BYTES])
            if dropped and not self.output_drop_reported:
                self.output_drop_reported = True
                print("[audio] speaker backlog was trimmed", file=sys.stderr)

    def speaker_worker(self):
        try:
            self.speaker = self.sound.RawOutputStream(
                samplerate=RATE,
                dtype="int16",
                channels=1,
                device=self.output_device,
                latency=0.08,
            )
            self.speaker.start()
            while not self.stopping.is_set() or not self.output.empty():
                try:
                    block = self.output.get(timeout=0.05)
                except queue.Empty:
                    continue
                self.writing.set()
                underflowed = self.speaker.write(block)
                self.writing.clear()
                if underflowed and not self.output_underflow_reported:
                    self.output_underflow_reported = True
                    print("[audio] speaker underflow was detected", file=sys.stderr)
        except Exception as exc:
            self.loop.call_soon_threadsafe(self.fail, exc)
        finally:
            self.writing.clear()
            if self.speaker is not None:
                with suppress(Exception):
                    self.speaker.stop()
                with suppress(Exception):
                    self.speaker.close()

    def start(self):
        self.speaker_thread = threading.Thread(
            target=self.speaker_worker,
            name="callbutton-memory-speaker",
            daemon=True,
        )
        self.speaker_thread.start()
        try:
            self.mic = self.sound.RawInputStream(
                samplerate=RATE,
                blocksize=RATE * CHUNK_MS // 1000,
                dtype="int16",
                channels=1,
                device=self.input_device,
                callback=self.input_callback,
                finished_callback=self.input_finished,
                latency="low",
            )
            self.mic.start()
        except Exception:
            self.close(abort=True)
            raise

    def stop_input(self):
        self.input_stopping.set()
        if self.mic is not None:
            with suppress(Exception):
                self.mic.stop()
            with suppress(Exception):
                self.mic.close()
            self.mic = None

    async def drain(self):
        try:
            async with asyncio.timeout(1):
                while not self.output.empty() or self.writing.is_set():
                    await asyncio.sleep(0.01)
            return True
        except TimeoutError:
            return False

    def close(self, abort):
        self.stop_input()
        if abort:
            with suppress(queue.Empty):
                while True:
                    self.output.get_nowait()
        self.stopping.set()
        if self.speaker_thread is not None:
            self.speaker_thread.join(timeout=2)
            if self.speaker_thread.is_alive() and self.speaker is not None:
                with suppress(Exception):
                    self.speaker.abort()
                self.speaker_thread.join(timeout=2)
        if not self.failed.done():
            self.failed.cancel()
