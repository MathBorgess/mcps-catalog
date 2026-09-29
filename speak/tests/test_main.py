"""Tests for speak_mcp.__main__: CLI dispatch and shutdown cleanup (M1).

Never plays real audio: srv.run() and LocalSink are monkeypatched/faked
throughout. The one real subprocess test sends SIGTERM to a freshly spawned
server that never received a speak/speak_clips call, so no LocalSink was
ever built and nothing can play.
"""

import signal
import subprocess
import sys

from speak_mcp import __main__ as main_module
from speak_mcp import server
from speak_mcp.sinks import LocalSink


# -- _stop_local_sink: best-effort shutdown cleanup ----------------------------


def test_stop_local_sink_stops_a_built_local_sink(monkeypatch):
    fake = LocalSink.__new__(LocalSink)  # a real LocalSink instance, no worker thread started
    calls = []
    fake.stop = lambda: calls.append(1)
    monkeypatch.setattr(server, "_sink", fake)
    main_module._stop_local_sink()
    assert calls == [1]


def test_stop_local_sink_is_a_noop_when_no_sink_was_built(monkeypatch):
    monkeypatch.setattr(server, "_sink", None)
    main_module._stop_local_sink()  # must not raise


def test_stop_local_sink_ignores_non_local_sinks(monkeypatch):
    class FakeTelegramSink:
        def stop(self):
            raise AssertionError("must never be called for a non-LocalSink")

    monkeypatch.setattr(server, "_sink", FakeTelegramSink())
    main_module._stop_local_sink()  # must not raise, must not call stop()


def test_stop_local_sink_swallows_exceptions_and_never_raises(monkeypatch):
    class ExplodingSink(LocalSink):
        def __init__(self):
            pass  # skip LocalSink.__init__ (no worker thread, no queue)

        def stop(self):
            raise RuntimeError("player boom")

    monkeypatch.setattr(server, "_sink", ExplodingSink())
    main_module._stop_local_sink()  # must not raise


def test_stop_local_sink_never_writes_to_stdout(monkeypatch, capsys):
    class ExplodingSink(LocalSink):
        def __init__(self):
            pass

        def stop(self):
            raise RuntimeError("player boom")

    monkeypatch.setattr(server, "_sink", ExplodingSink())
    main_module._stop_local_sink()
    captured = capsys.readouterr()
    assert captured.out == ""


# -- main(): stops the sink after srv.run() returns normally ------------------


def test_main_stops_local_sink_after_srv_run_returns(monkeypatch):
    calls = []
    monkeypatch.setattr(main_module, "_stop_local_sink", lambda: calls.append(1))
    monkeypatch.setattr(main_module.srv, "run", lambda: None)

    result = main_module.main([])

    assert result == 0
    assert calls == [1]


def test_main_stops_local_sink_even_if_srv_run_raises(monkeypatch):
    calls = []
    monkeypatch.setattr(main_module, "_stop_local_sink", lambda: calls.append(1))

    def _boom():
        raise RuntimeError("stdio closed unexpectedly")

    monkeypatch.setattr(main_module.srv, "run", _boom)

    try:
        main_module.main([])
    except RuntimeError:
        pass
    assert calls == [1]


# -- main(): registers a SIGTERM handler that also stops the sink -------------


def test_main_registers_sigterm_handler_before_running(monkeypatch):
    registered = {}

    def fake_signal(signum, handler):
        registered[signum] = handler

    monkeypatch.setattr(main_module.signal, "signal", fake_signal)
    monkeypatch.setattr(main_module.srv, "run", lambda: None)
    monkeypatch.setattr(main_module, "_stop_local_sink", lambda: None)

    main_module.main([])

    assert signal.SIGTERM in registered


def test_sigterm_handler_stops_local_sink(monkeypatch):
    calls = []
    monkeypatch.setattr(main_module, "_stop_local_sink", lambda: calls.append(1))
    try:
        main_module._handle_sigterm(signal.SIGTERM, None)
    except SystemExit:
        pass
    assert calls == [1]


# -- setup/say/sample: no signal handler or shutdown hook for one-shot modes --


def test_main_setup_mode_does_not_touch_signal_or_srv_run(monkeypatch):
    monkeypatch.setattr(main_module, "_cmd_setup", lambda: 0)
    calls = []
    monkeypatch.setattr(main_module.signal, "signal", lambda *a: calls.append(a))
    monkeypatch.setattr(main_module.srv, "run", lambda: calls.append("run"))

    result = main_module.main(["--setup"])

    assert result == 0
    assert calls == []


# -- real subprocess: SIGTERM before any tool call must exit promptly, and ----
# -- never leaves a stray line on stdout (the MCP protocol channel) ----------


def test_sigterm_before_any_call_exits_promptly_and_stdout_stays_clean():
    proc = subprocess.Popen(
        [sys.executable, "-m", "speak_mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    try:
        import time
        time.sleep(0.3)  # let the interpreter finish importing and reach srv.run()
        proc.send_signal(signal.SIGTERM)
        try:
            out, _err = proc.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _err = proc.communicate(timeout=5.0)
            raise AssertionError("server did not exit promptly on SIGTERM")
        assert out == ""  # nothing was ever written to stdout
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
