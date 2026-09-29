"""Tests for speak_mcp.sinks: local async player and Telegram sink.

All fakes are in-process and instantaneous: no real audio playback (never
invoke afplay/paplay/aplay), no network, no models. LocalSink spawns a real
daemon worker thread; tests synchronize on it with LocalSink.join() and, for
the stop() test, a threading.Event the fake player blocks on.
"""

import os
import subprocess
import threading
import time

import numpy as np
import pytest

from speak_mcp.clips import Clip
from speak_mcp.errors import ConfigError
from speak_mcp.sinks import (
    ClipResult,
    LocalSink,
    SpeakResult,
    SubprocessPlayer,
    TelegramSink,
    choose_sink,
    wait_seconds_from_env,
)

SR = 24000


class FakeSynth:
    """Zero-filled audio; can be told to raise for specific input texts."""

    def __init__(self, fail_on: set[str] | None = None, sr: int = SR, n_samples: int = 10):
        self.calls: list[str] = []
        self._fail_on = fail_on or set()
        self._sr = sr
        self._n = n_samples

    def synthesize(self, text: str, voice: str | None = None) -> tuple[np.ndarray, int]:
        self.calls.append(text)
        if text in self._fail_on:
            raise RuntimeError(f"synth failed for {text!r}")
        return np.zeros(self._n, dtype=np.float32), self._sr


class FakePlayer:
    """Records played paths (and whether the file existed at play time).
    Optionally blocks in play_file on an Event, to test stop()."""

    def __init__(self, block_event: threading.Event | None = None, resolve_error: Exception | None = None):
        self.played: list[str] = []
        self.existed_when_played: list[bool] = []
        self.stopped = False
        self._block_event = block_event
        self._resolve_error = resolve_error
        self.resolve_calls = 0

    def resolve(self) -> str:
        self.resolve_calls += 1
        if self._resolve_error is not None:
            raise self._resolve_error
        return "/fake/player"

    def play_file(self, path: str) -> None:
        self.existed_when_played.append(os.path.exists(path))
        self.played.append(path)
        if self._block_event is not None:
            self._block_event.wait(timeout=2.0)

    def stop(self) -> None:
        self.stopped = True
        if self._block_event is not None:
            self._block_event.set()


class FakeSender:
    def __init__(self, fail_message: bool = False, fail_voice_on: frozenset = frozenset()):
        self.messages: list[str] = []
        self.voices: list[tuple[bytes, str, int]] = []
        self._fail_message = fail_message
        self._fail_voice_on = fail_voice_on
        self._voice_call_count = 0

    def send_message(self, text: str) -> None:
        if self._fail_message:
            raise RuntimeError("telegram down")
        self.messages.append(text)

    def send_voice(self, ogg: bytes, caption: str, duration: int) -> None:
        index = self._voice_call_count
        self._voice_call_count += 1
        if index in self._fail_voice_on:
            raise RuntimeError("voice send failed")
        self.voices.append((ogg, caption, duration))


# -- choose_sink -------------------------------------------------------------


@pytest.mark.parametrize("env,expected", [
    ({"SPEAK_SINK": "local"}, "local"),
    ({"SPEAK_SINK": "telegram"}, "telegram"),
    ({}, "local"),
    ({"CLAUDE_CODE_REMOTE_SESSION_ID": "abc"}, "telegram"),
    ({"CLAUDE_CODE_REMOTE": "1"}, "telegram"),
    ({"SPEAK_SINK": "local", "CLAUDE_CODE_REMOTE": "1"}, "local"),  # explicit wins over auto
])
def test_choose_sink_matrix(env, expected):
    assert choose_sink(env) == expected


def test_choose_sink_invalid_value_is_config_error():
    with pytest.raises(ConfigError):
        choose_sink({"SPEAK_SINK": "carrier-pigeon"})


# -- LocalSink: play() contract ----------------------------------------------


def test_play_returns_immediately_queued():
    sink = LocalSink(FakeSynth(), player=FakePlayer())
    result = sink.play([Clip(text="oi"), Clip(text="tudo bem")], voice="pf_dora")
    assert result.ok is True
    assert result.sink == "local"
    assert result.queued is True
    assert [c.model_dump() for c in result.clips] == [
        {"index": 0, "ok": True, "seconds": 0.0, "error": None, "pending": False},
        {"index": 1, "ok": True, "seconds": 0.0, "error": None, "pending": False},
    ]
    assert sink.join(timeout=2.0)


def test_play_resolves_player_before_queueing():
    player = FakePlayer()
    sink = LocalSink(FakeSynth(), player=player)
    sink.play([Clip(text="oi")], voice=None)
    assert player.resolve_calls == 1
    assert sink.join(timeout=2.0)


def test_play_resolves_once_per_call_not_once_per_clip():
    player = FakePlayer()
    sink = LocalSink(FakeSynth(), player=player)
    sink.play([Clip(text="um"), Clip(text="dois"), Clip(text="tres")], voice=None)
    assert player.resolve_calls == 1
    assert sink.join(timeout=2.0)


def test_play_raises_config_error_when_no_player_binary_and_never_queues():
    """Regression I2b: a missing player binary must surface as a ConfigError
    from play() itself (-> ok:false from the server), not a silent
    ok:true/queued result that the background worker later fails on its own,
    invisibly to the caller."""
    player = FakePlayer(resolve_error=ConfigError("no audio player found"))
    sink = LocalSink(FakeSynth(), player=player)
    with pytest.raises(ConfigError, match="no audio player found"):
        sink.play([Clip(text="oi")], voice=None)
    assert sink._queue.qsize() == 0


def test_play_resolve_uses_real_subprocess_player_with_no_binary_on_path(monkeypatch):
    """End-to-end with the real SubprocessPlayer (not a fake): shutil.which
    finds nothing, so resolve() -- called by LocalSink.play() -- raises
    ConfigError before anything is enqueued."""
    player = SubprocessPlayer(which=lambda name: None)
    sink = LocalSink(FakeSynth(), player=player)
    with pytest.raises(ConfigError):
        sink.play([Clip(text="oi")], voice=None)
    assert sink._queue.qsize() == 0


def test_play_resolve_uses_real_subprocess_player_caches_binary(monkeypatch):
    """resolve() caches the binary on the real SubprocessPlayer, so a second
    play() call does not re-scan PATH."""
    calls = []

    def which(name):
        calls.append(name)
        return "/bin/afplay" if name == "afplay" else None

    player = SubprocessPlayer(which=which)
    sink = LocalSink(FakeSynth(), player=player)
    sink.play([Clip(text="um")], voice=None)
    assert sink.join(timeout=2.0)
    first_call_count = len(calls)
    sink.play([Clip(text="dois")], voice=None)
    assert sink.join(timeout=2.0)
    assert len(calls) == first_call_count  # no new shutil.which() calls: cached


# -- LocalSink: FIFO order across calls ---------------------------------------


def test_queue_order_across_two_play_calls():
    synth = FakeSynth()
    sink = LocalSink(synth, player=FakePlayer())
    sink.play([Clip(text="um")], voice=None)
    sink.play([Clip(text="dois"), Clip(text="tres")], voice=None)
    assert sink.join(timeout=2.0)
    assert synth.calls == ["um", "dois", "tres"]


# -- LocalSink: worker resilience ---------------------------------------------


def test_worker_survives_synth_exception_and_processes_next_job():
    synth = FakeSynth(fail_on={"vai falhar"})
    player = FakePlayer()
    sink = LocalSink(synth, player=player)
    sink.play([Clip(text="vai falhar")], voice=None)
    sink.play([Clip(text="depois funciona")], voice=None)
    assert sink.join(timeout=2.0)
    assert len(player.played) == 1  # only the second clip reached play_file
    assert synth.calls == ["vai falhar", "depois funciona"]


def test_worker_error_logged_to_stderr_never_stdout(capsys):
    synth = FakeSynth(fail_on={"boom"})
    sink = LocalSink(synth, player=FakePlayer())
    sink.play([Clip(text="boom")], voice=None)
    assert sink.join(timeout=2.0)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "boom" in captured.err


# -- LocalSink: temp file lifecycle -------------------------------------------


def test_temp_wav_files_exist_during_play_and_deleted_after():
    player = FakePlayer()
    sink = LocalSink(FakeSynth(), player=player)
    sink.play([Clip(text="curto")], voice=None)
    assert sink.join(timeout=2.0)
    assert len(player.played) == 1
    assert all(player.existed_when_played)
    assert not os.path.exists(player.played[0])


# -- LocalSink: stop() ---------------------------------------------------------


def test_stop_drains_pending_and_stops_current():
    block = threading.Event()
    synth = FakeSynth()
    player = FakePlayer(block_event=block)
    sink = LocalSink(synth, player=player)

    sink.play([Clip(text="tocando agora")], voice=None)
    deadline = time.monotonic() + 2.0
    while not player.played and time.monotonic() < deadline:
        time.sleep(0.005)
    assert player.played, "worker never started playing the first clip"

    sink.play([Clip(text="nunca deveria tocar")], voice=None)
    result = sink.stop()

    assert result.ok is True
    assert result.sink == "local"
    assert player.stopped is True
    assert sink.join(timeout=2.0)
    assert "nunca deveria tocar" not in synth.calls


def test_stop_returns_ok_result_when_nothing_is_playing():
    sink = LocalSink(FakeSynth(), player=FakePlayer())
    result = sink.stop()
    assert result.ok is True and result.sink == "local"


def test_sink_keeps_working_after_stop():
    block = threading.Event()
    block.set()  # never actually block playback in this test
    synth = FakeSynth()
    player = FakePlayer()
    sink = LocalSink(synth, player=player)
    sink.stop()
    sink.play([Clip(text="depois do stop")], voice=None)
    assert sink.join(timeout=2.0)
    assert "depois do stop" in synth.calls


# -- LocalSink: chunk pipelining -----------------------------------------------


def test_pipelines_next_chunk_synthesis_during_playback():
    started_second = threading.Event()

    class OrderSynth:
        def __init__(self):
            self.calls: list[str] = []

        def synthesize(self, text, voice=None):
            self.calls.append(text)
            if len(self.calls) == 2:
                started_second.set()
            return np.zeros(4, dtype=np.float32), SR

    class BlockingFirstPlayer:
        def __init__(self):
            self.played: list[str] = []

        def resolve(self) -> str:
            return "/fake/player"

        def play_file(self, path):
            self.played.append(path)
            if len(self.played) == 1:
                assert started_second.wait(timeout=2.0), (
                    "the next chunk should start synthesizing while the first one plays")

        def stop(self):
            pass

    sentence1 = ("word " * 60).strip() + "."
    sentence2 = ("term " * 60).strip() + "."
    text = f"{sentence1} {sentence2}"

    synth = OrderSynth()
    player = BlockingFirstPlayer()
    sink = LocalSink(synth, player=player)
    sink.play([Clip(text=text)], voice=None)
    assert sink.join(timeout=2.0)
    assert len(player.played) == 2
    assert len(synth.calls) == 2


# -- SubprocessPlayer (binary resolution only -- never spawns a real player) --


def test_subprocess_player_prefers_afplay_over_others():
    player = SubprocessPlayer(which=lambda name: f"/bin/{name}")
    assert player._resolve() == "/bin/afplay"


def test_subprocess_player_falls_back_to_paplay():
    player = SubprocessPlayer(which=lambda name: "/usr/bin/paplay" if name == "paplay" else None)
    assert player._resolve() == "/usr/bin/paplay"


def test_subprocess_player_falls_back_to_aplay():
    player = SubprocessPlayer(which=lambda name: "/usr/bin/aplay" if name == "aplay" else None)
    assert player._resolve() == "/usr/bin/aplay"


def test_subprocess_player_raises_config_error_when_none_found():
    player = SubprocessPlayer(which=lambda name: None)
    with pytest.raises(ConfigError):
        player._resolve()


def test_subprocess_player_play_file_raises_config_error_when_no_binary(tmp_path):
    player = SubprocessPlayer(which=lambda name: None)
    with pytest.raises(ConfigError):
        player.play_file(str(tmp_path / "x.wav"))


def test_subprocess_player_stop_without_active_process_is_a_noop():
    player = SubprocessPlayer(which=lambda name: None)
    player.stop()  # must not raise, even though nothing has ever played


def test_subprocess_player_stop_terminates_process_created_concurrently(monkeypatch):
    """Regression for the play_file/stop race (F1): Popen() creation and
    registering self._proc must be atomic under the lock, so a stop() that
    runs concurrently with play_file can never observe a stale/None _proc
    and let the just-started process run to completion unstoppably.

    subprocess.Popen is monkeypatched with a fake that blocks inside the
    "Popen call" until the test releases it, simulating the exact window
    stop() must not be able to sneak through. Never spawns a real process.
    """
    popen_entered = threading.Event()
    release_popen = threading.Event()
    created: list["FakeProc"] = []

    class FakeProc:
        def __init__(self):
            self.terminated = threading.Event()

        def wait(self):
            self.terminated.wait(timeout=2.0)

        def poll(self):
            return None if not self.terminated.is_set() else -15

        def terminate(self):
            self.terminated.set()

    def fake_popen(args, **kwargs):
        popen_entered.set()
        # Held here to simulate the gap between the process existing and
        # self._proc being set. With Popen()+registration atomic under the
        # lock, a concurrent stop() blocks on that same lock and can only
        # proceed once this call (and the registration) has completed.
        release_popen.wait(timeout=2.0)
        proc = FakeProc()
        created.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    player = SubprocessPlayer(which=lambda name: "/bin/afplay")

    play_thread = threading.Thread(target=player.play_file, args=("clip.wav",))
    play_thread.start()
    assert popen_entered.wait(timeout=1.0), "play_file never reached Popen()"

    stop_thread = threading.Thread(target=player.stop)
    stop_thread.start()
    time.sleep(0.05)  # give stop() a chance to actually block on the lock
    release_popen.set()

    play_thread.join(timeout=2.0)
    stop_thread.join(timeout=2.0)

    assert not play_thread.is_alive()
    assert not stop_thread.is_alive()
    assert len(created) == 1
    assert created[0].terminated.is_set(), "stop() must terminate the process play_file just created"


# -- TelegramSink: note-first, ordering ---------------------------------------


def test_telegram_note_sent_before_clip_voices():
    sender = FakeSender()
    sink = TelegramSink(FakeSynth(), sender)
    result = sink.play([Clip(text="oi", caption="c1")], voice=None, note="edição 42")
    assert sender.messages == ["edição 42"]
    assert len(sender.voices) == 1
    assert result.ok is True
    assert result.sink == "telegram"


def test_telegram_no_note_sends_no_message():
    sender = FakeSender()
    sink = TelegramSink(FakeSynth(), sender)
    sink.play([Clip(text="oi")], voice=None)
    assert sender.messages == []


# -- TelegramSink: note failure -----------------------------------------------


def test_telegram_note_failure_fails_all_clips_without_sending_voices():
    sender = FakeSender(fail_message=True)
    sink = TelegramSink(FakeSynth(), sender)
    result = sink.play([Clip(text="a"), Clip(text="b")], voice=None, note="nota")
    assert result.ok is False
    assert sender.voices == []
    assert len(result.clips) == 2
    for clip_result in result.clips:
        assert clip_result.ok is False
        assert clip_result.error is not None and clip_result.error.startswith("note:")
    assert result.message is not None and result.message.startswith("note:")


# -- TelegramSink: per-clip isolation ------------------------------------------


def test_telegram_per_clip_isolation():
    sender = FakeSender(fail_voice_on=frozenset({0}))  # first send_voice call fails
    sink = TelegramSink(FakeSynth(), sender)
    result = sink.play([Clip(text="falha"), Clip(text="funciona")], voice=None)
    assert result.ok is False
    assert result.clips[0].ok is False and result.clips[0].error is not None
    assert result.clips[1].ok is True
    assert len(sender.voices) == 1  # only the second clip's voice got through
    assert result.message == "1/2 clips failed"


def test_telegram_partial_failure_message_counts_correctly():
    """Regression M5: the top-level `message` must summarize a partial
    per-clip failure (e.g. "2/3 clips failed") so a caller like the
    radar-audio skill can report how many clips actually went through,
    not just that something failed."""
    sender = FakeSender(fail_voice_on=frozenset({0, 2}))  # 1st and 3rd fail, 2nd succeeds
    sink = TelegramSink(FakeSynth(), sender)
    result = sink.play(
        [Clip(text="um"), Clip(text="dois"), Clip(text="tres")], voice=None)
    assert result.ok is False
    assert result.message == "2/3 clips failed"


def test_telegram_all_clips_succeed_message_is_none():
    sender = FakeSender()
    sink = TelegramSink(FakeSynth(), sender)
    result = sink.play([Clip(text="um"), Clip(text="dois")], voice=None)
    assert result.ok is True
    assert result.message is None


# -- TelegramSink: caption default and duration rounding -----------------------


def test_telegram_caption_none_becomes_empty_string():
    sender = FakeSender()
    sink = TelegramSink(FakeSynth(), sender)
    sink.play([Clip(text="sem legenda", caption=None)], voice=None)
    _, caption, _ = sender.voices[0]
    assert caption == ""


def test_telegram_duration_is_rounded_seconds():
    synth = FakeSynth(n_samples=int(SR * 2.6))  # 2.6s of audio at 24kHz
    sender = FakeSender()
    sink = TelegramSink(synth, sender)
    sink.play([Clip(text="curto")], voice=None)
    _, _, duration = sender.voices[0]
    assert duration == 3
    assert isinstance(duration, int)


def test_telegram_duration_floor_is_one_second():
    synth = FakeSynth(n_samples=1)  # ~0 seconds of audio, must not round down to 0
    sender = FakeSender()
    sink = TelegramSink(synth, sender)
    sink.play([Clip(text="curtinho")], voice=None)
    _, _, duration = sender.voices[0]
    assert duration == 1


# -- TelegramSink: chunked synth concatenation ---------------------------------


class GrowingSynth:
    """Returns a distinct, increasing sample count per call (1000, 2000, ...)
    so a concatenation bug that keeps only the first or last chunk yields a
    different, easily distinguishable duration than the correct total."""

    def __init__(self, sr: int = SR):
        self.calls: list[str] = []
        self._sr = sr

    def synthesize(self, text: str, voice: str | None = None) -> tuple[np.ndarray, int]:
        self.calls.append(text)
        n = 1000 * len(self.calls)
        return np.zeros(n, dtype=np.float32), self._sr


def test_telegram_concatenates_multiple_chunks():
    long_text = ("frase numero um muito comprida " * 15).strip() + ". " + \
                ("frase numero dois tambem comprida " * 15).strip() + "."
    synth = GrowingSynth()
    sender = FakeSender()
    sink = TelegramSink(synth, sender)
    result = sink.play([Clip(text=long_text)], voice=None)
    assert result.clips[0].ok is True
    assert len(synth.calls) >= 2  # text was chunked and synthesized piece by piece

    # The reported seconds/duration must reflect the full concatenation of
    # every chunk's distinct sample count, not just the first or last chunk.
    expected_samples = sum(1000 * (i + 1) for i in range(len(synth.calls)))
    expected_seconds = expected_samples / SR
    assert result.clips[0].seconds == pytest.approx(expected_seconds)
    _, _, duration = sender.voices[0]
    assert duration == max(1, round(expected_seconds))
    # Sanity: a first-chunk-only or last-chunk-only regression would produce
    # a smaller, different duration than the true sum -- pin that down too.
    first_chunk_only_seconds = 1000 / SR
    last_chunk_only_seconds = (1000 * len(synth.calls)) / SR
    assert expected_seconds != first_chunk_only_seconds
    assert expected_seconds != last_chunk_only_seconds


# -- TelegramSink: soft deadline (the client's 60s tool timeout) ----------------


class GatedSynth(FakeSynth):
    """Blocks synthesis of the chunk "lento" until `release` is set."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def synthesize(self, text: str, voice: str | None = None) -> tuple[np.ndarray, int]:
        if text == "lento":
            assert self.release.wait(5.0), "test never released the slow clip"
        return super().synthesize(text, voice)


def test_telegram_slow_delivery_answers_queued_and_keeps_sending():
    synth, sender = GatedSynth(), FakeSender()
    sink = TelegramSink(synth, sender, wait_seconds=0.05)
    t0 = time.monotonic()
    result = sink.play([Clip(text="rapido"), Clip(text="lento"), Clip(text="depois")],
                       voice=None, note="edição")
    assert time.monotonic() - t0 < 2.0  # answered at the deadline, not at the end
    assert result.ok is True and result.queued is True and result.sink == "telegram"
    assert [(r.index, r.pending) for r in result.clips] == [(0, False), (1, True), (2, True)]
    assert result.clips[0].seconds > 0
    assert result.message == ("1/3 clips sent, 2 still sending in the background "
                              "(their results are not reported)")

    synth.release.set()
    assert sink.join(timeout=5.0) is True
    assert sender.messages == ["edição"]
    assert len(sender.voices) == 3  # nothing was dropped after answering


def test_telegram_deadline_zero_answers_immediately_with_everything_pending():
    synth, sender = GatedSynth(), FakeSender()
    sink = TelegramSink(synth, sender, wait_seconds=0)
    result = sink.play([Clip(text="lento")], voice=None)
    assert result.queued is True and result.ok is True
    assert [r.pending for r in result.clips] == [True]
    synth.release.set()
    assert sink.join(timeout=5.0) is True
    assert len(sender.voices) == 1


def test_telegram_within_deadline_still_returns_the_full_report():
    sender = FakeSender(fail_voice_on=frozenset({1}))
    sink = TelegramSink(FakeSynth(), sender, wait_seconds=30.0)
    result = sink.play([Clip(text="a"), Clip(text="b")], voice=None)
    assert result.queued is False
    assert [r.pending for r in result.clips] == [False, False]
    assert result.ok is False and result.message == "1/2 clips failed"


def test_telegram_failure_before_the_deadline_is_reported_with_pending_rest():
    synth, sender = GatedSynth(), FakeSender(fail_voice_on=frozenset({0}))
    sink = TelegramSink(synth, sender, wait_seconds=0.2)
    result = sink.play([Clip(text="falha"), Clip(text="lento")], voice=None)
    assert result.queued is True and result.ok is False
    assert result.clips[0].ok is False and result.clips[0].error is not None
    assert result.clips[1].pending is True
    assert result.message == ("1/2 clips failed; 0/2 clips sent, 1 still sending in the "
                              "background (their results are not reported)")
    synth.release.set()
    assert sink.join(timeout=5.0) is True


def test_telegram_jobs_are_delivered_in_call_order():
    synth, sender = GatedSynth(), FakeSender()
    sink = TelegramSink(synth, sender, wait_seconds=0.05)
    first = sink.play([Clip(text="lento", caption="um")], voice=None)
    second = sink.play([Clip(text="rapido", caption="dois")], voice=None)
    assert first.queued is True and second.queued is True
    synth.release.set()
    assert sink.join(timeout=5.0) is True
    assert [caption for _ogg, caption, _dur in sender.voices] == ["um", "dois"]


def test_telegram_failure_after_the_answer_goes_to_stderr_never_stdout(capsys):
    synth, sender = GatedSynth(), FakeSender(fail_voice_on=frozenset({0}))
    sink = TelegramSink(synth, sender, wait_seconds=0.05)
    result = sink.play([Clip(text="lento")], voice=None)
    assert result.queued is True
    synth.release.set()
    assert sink.join(timeout=5.0) is True
    deadline = time.monotonic() + 2.0
    err = ""
    while "failed after the tool answered" not in err and time.monotonic() < deadline:
        err += capsys.readouterr().err
        time.sleep(0.01)
    assert "1/1 clips failed" in err
    assert capsys.readouterr().out == ""


def test_telegram_join_times_out_while_a_job_is_still_running():
    synth = GatedSynth()
    sink = TelegramSink(synth, FakeSender(), wait_seconds=0)
    sink.play([Clip(text="lento")], voice=None)
    assert sink.join(timeout=0.05) is False
    synth.release.set()
    assert sink.join(timeout=5.0) is True


def test_telegram_join_returns_immediately_when_idle():
    assert TelegramSink(FakeSynth(), FakeSender()).join(timeout=0.05) is True


def test_telegram_worker_survives_an_unexpected_delivery_error(monkeypatch, capsys):
    sink = TelegramSink(FakeSynth(), FakeSender())
    real = sink._deliver
    calls = []

    def flaky(job):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(job)

    monkeypatch.setattr(sink, "_deliver", flaky)
    first = sink.play([Clip(text="a")], voice=None)
    assert first.ok is False and first.message == "worker: boom"
    assert "telegram sink worker error: boom" in capsys.readouterr().err
    assert sink.play([Clip(text="b")], voice=None).ok is True


# -- wait_seconds_from_env --------------------------------------------------------


def test_wait_seconds_defaults_to_45():
    assert wait_seconds_from_env({}) == 45.0
    assert wait_seconds_from_env({"SPEAK_WAIT_SECONDS": "  "}) == 45.0


@pytest.mark.parametrize("raw,expected", [("0", 0.0), ("30", 30.0), ("12.5", 12.5), ("3600", 3600.0)])
def test_wait_seconds_accepts_a_number_in_range(raw, expected):
    assert wait_seconds_from_env({"SPEAK_WAIT_SECONDS": raw}) == expected


@pytest.mark.parametrize("raw", ["abc", "-1", "3601", "nan", "inf"])
def test_wait_seconds_rejects_bad_values_as_config_error(raw):
    with pytest.raises(ConfigError, match="SPEAK_WAIT_SECONDS"):
        wait_seconds_from_env({"SPEAK_WAIT_SECONDS": raw})


# -- TelegramSink: stop() -------------------------------------------------------


def test_telegram_stop_is_a_noop_result():
    sink = TelegramSink(FakeSynth(), FakeSender())
    result = sink.stop()
    assert result == SpeakResult(ok=True, sink="telegram", message="nothing to stop on telegram")


# -- Result model shapes ---------------------------------------------------------


def test_clip_result_defaults():
    r = ClipResult(index=0, ok=True)
    assert r.seconds == 0.0 and r.error is None and r.pending is False


def test_speak_result_defaults():
    r = SpeakResult(ok=True, sink="local")
    assert r.queued is False and r.clips == [] and r.message is None
