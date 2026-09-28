"""CLI entry point for speak-mcp.

No args: run the stdio MCP server (never prints to stdout).
--say/--sample/--setup: manual smoke-test modes; these may print to stdout.
"""

import argparse
import signal
import sys

from speak_mcp import server
from speak_mcp.clips import Clip
from speak_mcp.errors import ConfigError
from speak_mcp.server import make_synth, srv
from speak_mcp.sinks import LocalSink
from speak_mcp.synth import KokoroSynth, Settings, encode_ogg, ensure_models


def _cmd_say(text: str, voice: str | None) -> int:
    try:
        sink = LocalSink(make_synth())
        sink.play([Clip(text=text)], voice)
        sink.join(timeout=600.0)
    except ConfigError as exc:
        print(f"speak-mcp: config error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_sample(text: str, out: str, voice: str | None) -> int:
    try:
        samples, sample_rate = make_synth().synthesize(text, voice)
    except ConfigError as exc:
        print(f"speak-mcp: config error: {exc}", file=sys.stderr)
        return 1
    with open(out, "wb") as f:
        f.write(encode_ogg(samples, sample_rate))
    print(f"speak-mcp: wrote {out}")
    return 0


def _cmd_setup() -> int:
    try:
        settings = Settings.from_env()
        ensure_models(settings)
        KokoroSynth(settings)
    except ConfigError as exc:
        print(f"speak-mcp: setup failed: {exc}", file=sys.stderr)
        return 1
    print("speak-mcp: setup complete")
    return 0


def _stop_local_sink() -> None:
    """Best-effort: stop the process-wide LocalSink's background player and
    clear its queue, if the server ever built one (terminates the current
    afplay/paplay/aplay child and drops pending jobs). Called after srv.run()
    returns and from the SIGTERM handler below. Never touches stdout -- the
    stdio MCP protocol channel -- so any failure here is swallowed silently
    (regression: M1)."""
    try:
        sink = server._sink
        if isinstance(sink, LocalSink):
            sink.stop()
    except Exception:
        pass


def _handle_sigterm(signum, frame) -> None:
    _stop_local_sink()
    raise SystemExit(0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="speak-mcp")
    parser.add_argument("--say", metavar="TEXT",
                         help="synthesize and play locally, blocking until done (manual smoke test)")
    parser.add_argument("--sample", metavar="TEXT",
                         help="synthesize to an OGG file without playing (use with --out)")
    parser.add_argument("--out", metavar="FILE", help="output path for --sample")
    parser.add_argument("--voice", help="Kokoro voice id (default: SPEAK_VOICE or pf_dora)")
    parser.add_argument("--setup", action="store_true",
                         help="download models and warm the synth, then exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.setup:
        return _cmd_setup()
    if args.say is not None:
        return _cmd_say(args.say, args.voice)
    if args.sample is not None:
        if not args.out:
            print("speak-mcp: --sample requires --out", file=sys.stderr)
            return 2
        return _cmd_sample(args.sample, args.out, args.voice)

    signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        srv.run()
    finally:
        _stop_local_sink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
