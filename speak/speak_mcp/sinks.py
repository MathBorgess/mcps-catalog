"""Sinks: async local audio playback (afplay/paplay/aplay) and a Telegram
voice-message sink that answers within a soft deadline. Runs inside a stdio MCP
server: never write to stdout; worker errors are logged to stderr only.
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
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
from pydantic import BaseModel

from speak_mcp.clips import Clip, chunk_text
from speak_mcp.errors import ConfigError
from speak_mcp.synth import encode_ogg, encode_wav

PLAYER_BINARIES = ("afplay", "paplay", "aplay")

# Claude Code gives an MCP tool call 60s before it gives up on the reply, and a
# radar-sized batch of clips takes minutes to synthesize on the cloud VM. The
# Telegram sink therefore answers after this long even if delivery is still
# running (see TelegramSink); 45s leaves headroom under that 60s limit.
DEFAULT_WAIT_SECONDS = 45.0
MAX_WAIT_SECONDS = 3600.0


class Synth(Protocol):
    def synthesize(self, text: str, voice: str | None = None) -> tuple[np.ndarray, int]: ...


class Player(Protocol):
    def play_file(self, path: str) -> None: ...
    def stop(self) -> None: ...
    def resolve(self) -> str: ...


class Sender(Protocol):
    def send_message(self, text: str) -> None: ...
    def send_voice(self, ogg: bytes, caption: str, duration: int) -> None: ...


class ClipResult(BaseModel):
    index: int
    ok: bool
    seconds: float = 0.0
    error: str | None = None
    # True when the clip was accepted but not yet delivered at the time the
    # tool answered (Telegram soft deadline); `ok` is then not a delivery receipt.
    pending: bool = False


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


def wait_seconds_from_env(env: Mapping[str, str] = os.environ) -> float:
    """SPEAK_WAIT_SECONDS: how long a Telegram call waits for delivery before
    answering `queued`. 0 means answer immediately."""
    raw = env.get("SPEAK_WAIT_SECONDS")
    if not raw or not raw.strip():
        return DEFAULT_WAIT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError("SPEAK_WAIT_SECONDS must be a number") from None
    if not 0 <= value <= MAX_WAIT_SECONDS:
        raise ConfigError(f"SPEAK_WAIT_SECONDS must be 0-{MAX_WAIT_SECONDS:g}")
    return value


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

    def resolve(self) -> str:
        """Public wrapper around _resolve(): LocalSink.play() calls this
        eagerly (before queueing anything) so a missing player binary
        surfaces as a ConfigError -> ok:false tool result, instead of only
        failing later inside the background worker (regression: I2b)."""
        return self._resolve()

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

    def play(self, clips: list[Clip], voice: str | None, note: str | None = None) -> SpeakResult:
        # `note` is part of the uniform Sink interface (TelegramSink sends it
        # as a leading plain-text message) but local playback has nothing
        # analogous to send it as, so it is accepted and ignored here.
        del note
        # Resolve (and cache) the player binary before queueing anything: the
        # worker thread only discovers a missing player when it dequeues the
        # job, by which point play() has already returned ok:true/queued to
        # the caller -- a silent success that never actually plays anything.
        # Failing fast here lets ConfigError propagate to the server's
        # _dispatch(), which turns it into ok:false (regression: I2b).
        self._player.resolve()
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


@dataclass
class _Job:
    clips: list[Clip]
    voice: str | None
    note: str | None
    progress: list[ClipResult] = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)
    result: SpeakResult | None = None
    detached: bool = False  # play() already answered `queued`; nobody reads `result`


class TelegramSink:
    """Telegram delivery: one OGG/Opus voice message per clip.

    A single daemon worker delivers jobs FIFO (across calls, so messages keep
    their order and synthesis never runs twice at once). play() waits for its
    job for up to `wait_seconds`: if it finishes in time the result is the full
    per-clip report; otherwise play() answers `queued=True` with what has been
    delivered so far and the rest keeps going in the background (failures then
    only reach stderr, like LocalSink's worker)."""

    def __init__(self, synth: Synth, sender: Sender, wait_seconds: float = DEFAULT_WAIT_SECONDS):
        self._synth = synth
        self._sender = sender
        self._wait = wait_seconds
        self._jobs: "queue.Queue[_Job]" = queue.Queue()
        self._worker = threading.Thread(target=self._run, name="speak-mcp-telegram-sink", daemon=True)
        self._worker.start()

    def play(self, clips: list[Clip], voice: str | None, note: str | None = None) -> SpeakResult:
        job = _Job(clips=clips, voice=voice, note=note)
        self._jobs.put(job)
        if job.done.wait(self._wait):
            return job.result
        job.detached = True
        if job.done.is_set():  # finished between the timeout and the flag: report it in full
            return job.result
        return self._pending_result(job)

    def join(self, timeout: float | None = None) -> bool:
        """Block until every enqueued job is delivered (or `timeout` elapses).
        Returns False on timeout. Used at shutdown so a normal exit does not
        cut off clips still being sent, and as a test hook."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._jobs.unfinished_tasks > 0:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                job.result = self._deliver(job)
            except Exception as exc:  # worker must survive; stdio protocol -> stderr only
                print(f"[speak-mcp] telegram sink worker error: {exc}", file=sys.stderr)
                job.result = SpeakResult(ok=False, sink="telegram", message=f"worker: {exc}")
            if job.detached and not job.result.ok:
                print(f"[speak-mcp] telegram delivery failed after the tool answered: "
                      f"{job.result.message}", file=sys.stderr)
            job.done.set()
            self._jobs.task_done()

    def _pending_result(self, job: _Job) -> SpeakResult:
        total = len(job.clips)
        finished = list(job.progress)
        sent = sum(1 for r in finished if r.ok)
        failed = len(finished) - sent
        remaining = total - len(finished)
        results = finished + [ClipResult(index=i, ok=True, pending=True)
                              for i in range(len(finished), total)]
        parts = []
        if failed:
            parts.append(f"{failed}/{total} clips failed")
        if remaining:
            parts.append(f"{sent}/{total} clips sent, {remaining} still sending in the "
                         "background (their results are not reported)")
        return SpeakResult(ok=failed == 0, sink="telegram", queued=remaining > 0,
                           clips=results, message="; ".join(parts) or None)

    def _deliver(self, job: _Job) -> SpeakResult:
        if job.note:
            try:
                self._sender.send_message(job.note)
            except Exception as exc:
                err = f"note: {exc}"
                results = [ClipResult(index=i, ok=False, error=err) for i in range(len(job.clips))]
                job.progress.extend(results)
                return SpeakResult(ok=False, sink="telegram", clips=results, message=err)

        overall_ok = True
        for i, clip in enumerate(job.clips):
            try:
                job.progress.append(self._play_one(i, clip, job.voice))
            except Exception as exc:  # per-clip isolation
                overall_ok = False
                job.progress.append(ClipResult(index=i, ok=False, error=str(exc)))
        results = list(job.progress)
        message = None
        if not overall_ok:
            # A per-clip failure (as opposed to the note failure above, which
            # already carries its own message) must be visible at the
            # top-level `message` too -- callers like the radar-audio skill
            # summarize a run from `message`, and "ok: false" alone doesn't
            # say how many of the clips actually went through
            # (regression: M5).
            failed = sum(1 for r in results if not r.ok)
            message = f"{failed}/{len(results)} clips failed"
        return SpeakResult(ok=overall_ok, sink="telegram", clips=results, message=message)

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
