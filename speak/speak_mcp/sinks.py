"""Sinks: async local audio playback (afplay/paplay/aplay) and a synchronous
Telegram voice-message sink. Runs inside a stdio MCP server: never write to
stdout; worker errors are logged to stderr only.
"""

import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from typing import Protocol

import numpy as np
from pydantic import BaseModel

from speak_mcp.clips import Clip, chunk_text
from speak_mcp.errors import ConfigError
from speak_mcp.synth import encode_ogg, encode_wav

PLAYER_BINARIES = ("afplay", "paplay", "aplay")


class Synth(Protocol):
    def synthesize(self, text: str, voice: str | None = None) -> tuple[np.ndarray, int]: ...


class Player(Protocol):
    def play_file(self, path: str) -> None: ...
    def stop(self) -> None: ...


class Sender(Protocol):
    def send_message(self, text: str) -> None: ...
    def send_voice(self, ogg: bytes, caption: str, duration: int) -> None: ...


class ClipResult(BaseModel):
    index: int
    ok: bool
    seconds: float = 0.0
    error: str | None = None


class SpeakResult(BaseModel):
    ok: bool
    sink: str
    queued: bool = False
    clips: list[ClipResult] = []
    message: str | None = None


def choose_sink(env: Mapping[str, str] = os.environ) -> str:
    """Pick "local" or "telegram". SPEAK_SINK wins when set (invalid -> ConfigError);
    otherwise auto-detect a cloud session via CLAUDE_CODE_REMOTE(_SESSION_ID)."""
    raw = env.get("SPEAK_SINK")
    if raw:
        if raw not in ("local", "telegram"):
            raise ConfigError(f"SPEAK_SINK must be 'local' or 'telegram', got {raw!r}")
        return raw
    if env.get("CLAUDE_CODE_REMOTE_SESSION_ID") or env.get("CLAUDE_CODE_REMOTE"):
        return "telegram"
    return "local"


class SubprocessPlayer:
    """Plays a file with the first of afplay/paplay/aplay found on PATH.
    Binary resolution happens lazily (at play time), not at construction."""

    def __init__(self, which=shutil.which):
        self._which = which
        self._binary: str | None = None
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    def _resolve(self) -> str:
        if self._binary is None:
            for name in PLAYER_BINARIES:
                path = self._which(name)
                if path:
                    self._binary = path
                    break
            else:
                raise ConfigError(
                    "no audio player found (looked for afplay, paplay, aplay on PATH)")
        return self._binary

    def play_file(self, path: str) -> None:
        binary = self._resolve()
        # Popen() and registering self._proc happen under the same lock a
        # concurrent stop() also acquires, so process creation is atomic
        # with becoming visible to stop(): there is no window in which a
        # process exists but self._proc doesn't reflect it yet, so stop()
        # can never observe a stale/None _proc while this process is live.
        with self._lock:
            proc = subprocess.Popen(
                [binary, path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._proc = proc
        try:
            proc.wait()
        finally:
            with self._lock:
                if self._proc is proc:
                    self._proc = None

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()


class LocalSink:
    """Async local playback. play() enqueues one job per clip and returns
    immediately; a single daemon worker thread plays jobs FIFO (across calls),
    pipelining synthesis one chunk ahead of playback."""

    def __init__(self, synth: Synth, player: Player | None = None):
        self._synth = synth
        self._player = player or SubprocessPlayer()
        self._queue: "queue.Queue[tuple[Clip, str | None]]" = queue.Queue()
        self._epoch_lock = threading.Lock()
        self._stop_epoch = 0
        self._worker = threading.Thread(target=self._run, name="speak-mcp-local-sink", daemon=True)
        self._worker.start()

    def play(self, clips: list[Clip], voice: str | None) -> SpeakResult:
        results = []
        for i, clip in enumerate(clips):
            self._queue.put((clip, voice))
            results.append(ClipResult(index=i, ok=True, seconds=0.0))
        return SpeakResult(ok=True, sink="local", queued=True, clips=results)

    def stop(self) -> SpeakResult:
        self._drain()
        with self._epoch_lock:
            self._stop_epoch += 1
        self._player.stop()
        return SpeakResult(ok=True, sink="local", message="stopped")

    def join(self, timeout: float = 5.0) -> bool:
        """Test hook: block until the queue is drained (or timeout elapses).
        Returns False on timeout, True once every enqueued job is done."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks > 0:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def _drain(self) -> None:
        try:
            while True:
                self._queue.get_nowait()
                self._queue.task_done()
        except queue.Empty:
            pass

    def _run(self) -> None:
        while True:
            clip, voice = self._queue.get()
            try:
                self._play_clip(clip, voice)
            except Exception as exc:  # worker must survive; stdio protocol -> stderr only
                print(f"[speak-mcp] local sink worker error: {exc}", file=sys.stderr)
            finally:
                self._queue.task_done()

    def _play_clip(self, clip: Clip, voice: str | None) -> None:
        with self._epoch_lock:
            epoch = self._stop_epoch
        chunks = chunk_text(clip.text)
        if not chunks:
            return
        tmpdir = tempfile.mkdtemp(prefix="speak-mcp-")
        try:
            current = self._synth.synthesize(chunks[0], voice)
            for i in range(len(chunks)):
                with self._epoch_lock:
                    if self._stop_epoch != epoch:
                        return
                samples, sr = current

                next_thread: threading.Thread | None = None
                holder: dict = {}
                if i + 1 < len(chunks):
                    next_text = chunks[i + 1]

                    def _synth_next() -> None:
                        try:
                            holder["value"] = self._synth.synthesize(next_text, voice)
                        except Exception as exc:
                            holder["error"] = exc

                    next_thread = threading.Thread(target=_synth_next, daemon=True)
                    next_thread.start()

                path = os.path.join(tmpdir, f"chunk-{i}.wav")
                with open(path, "wb") as f:
                    f.write(encode_wav(samples, sr))
                try:
                    self._player.play_file(path)
                finally:
                    if os.path.exists(path):
                        os.remove(path)

                if next_thread is not None:
                    next_thread.join()
                    if "error" in holder:
                        raise holder["error"]
                    current = holder["value"]
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TelegramSink:
    """Synchronous Telegram delivery: one OGG/Opus voice message per clip."""

    def __init__(self, synth: Synth, sender: Sender):
        self._synth = synth
        self._sender = sender

    def play(self, clips: list[Clip], voice: str | None, note: str | None = None) -> SpeakResult:
        if note:
            try:
                self._sender.send_message(note)
            except Exception as exc:
                err = f"note: {exc}"
                results = [ClipResult(index=i, ok=False, error=err) for i in range(len(clips))]
                return SpeakResult(ok=False, sink="telegram", clips=results, message=err)

        results = []
        overall_ok = True
        for i, clip in enumerate(clips):
            try:
                results.append(self._play_one(i, clip, voice))
            except Exception as exc:  # per-clip isolation
                overall_ok = False
                results.append(ClipResult(index=i, ok=False, error=str(exc)))
        return SpeakResult(ok=overall_ok, sink="telegram", clips=results)

    def _play_one(self, index: int, clip: Clip, voice: str | None) -> ClipResult:
        # Kokoro has a phoneme limit per call: chunk, synthesize each piece, concatenate.
        chunks = chunk_text(clip.text)
        pieces = []
        sr = None
        for chunk in chunks:
            samples, sr = self._synth.synthesize(chunk, voice)
            pieces.append(samples)
        combined = pieces[0] if len(pieces) == 1 else np.concatenate(pieces)
        ogg = encode_ogg(combined, sr)
        seconds = len(combined) / sr if sr else 0.0
        self._sender.send_voice(ogg, clip.caption or "", max(1, round(seconds)))
        return ClipResult(index=index, ok=True, seconds=seconds)

    def stop(self) -> SpeakResult:
        return SpeakResult(ok=True, sink="telegram", message="nothing to stop on telegram")
