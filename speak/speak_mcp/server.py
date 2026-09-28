"""speak MCP server: three tools (speak, speak_clips, speak_stop) over stdio.

Never print to stdout here — the stdio transport uses stdout for the MCP
protocol. Synth/sink construction is lazy (module-level singletons built on
first use) so the server starts fine without models or env configured.
"""

import asyncio
import threading

from mcp.server.mcpserver import MCPServer

from speak_mcp.clips import (
    MAX_CLIPS,
    MAX_CLIP_TEXT,
    MAX_NOTE,
    MAX_TEXT,
    Clip,
    label_error,
    text_error,
)
from speak_mcp.errors import ConfigError
from speak_mcp.sinks import LocalSink, SpeakResult, TelegramSink, choose_sink
from speak_mcp.synth import KokoroSynth, Settings, ensure_models, lang_for
from speak_mcp.telegram import Telegram

INSTRUCTIONS = """\
When the user asks to hear, listen to, or have something said or described in \
audio ("me fala em áudio", "read this to me", and similar), write the content \
as spoken prose for the ear before calling a tool: no markdown, no URLs, no \
tables or code; spell out numbers, units and acronyms the way they are spoken; \
write in the user's language (default Portuguese voice pf_dora; pick an \
English voice such as af_heart for English). Call `speak` for one piece of \
text. Call `speak_clips` for several independent parts said in sequence. Call \
`speak_stop` to stop local playback. On the Mac this plays immediately; in \
cloud sessions it arrives as a voice message on the owner's Telegram instead.
"""

srv = MCPServer("speak", instructions=INSTRUCTIONS)

# Both make_synth() and make_sink() guard their own singleton with this same
# lock. It is a plain (non-reentrant) Lock, so make_sink() must never call
# make_synth() while holding it -- that would be the same thread trying to
# acquire the lock twice and deadlock on the very first real tool call. See
# the ordering note in make_sink() below (regression: F1).
_lock = threading.Lock()
_synth: KokoroSynth | None = None
_sink: LocalSink | TelegramSink | None = None


def make_synth() -> KokoroSynth:
    """Lazily build (and cache) the process-wide Kokoro synth. Monkeypatchable in tests."""
    global _synth
    if _synth is None:
        with _lock:
            if _synth is None:
                settings = Settings.from_env()
                ensure_models(settings)
                _synth = KokoroSynth(settings)
    return _synth


def make_sink() -> LocalSink | TelegramSink:
    """Lazily build (and cache) the process-wide sink so `speak_stop` can reach
    the same local player across calls. Monkeypatchable in tests."""
    global _sink
    if _sink is None:
        # Build/fetch the synth *before* taking _lock: make_synth() takes the
        # same lock itself, and a plain Lock is not reentrant, so calling it
        # from inside `with _lock:` below would deadlock this thread against
        # itself on every fresh process's first call.
        synth = make_synth()
        with _lock:
            if _sink is None:
                kind = choose_sink()
                if kind == "local":
                    _sink = LocalSink(synth)
                else:
                    _sink = TelegramSink(synth, Telegram.from_env())
    return _sink


def _validate_voice(synth: KokoroSynth, voice: str) -> str | None:
    """Eagerly validate an explicit voice override before dispatch. LocalSink's
    worker fails an unknown voice silently in the background (regression: F3),
    so the server must catch it itself before ever reaching either sink."""
    try:
        lang_for(voice)
    except ConfigError as exc:
        return str(exc)
    if voice not in synth.voices():
        return f"unknown voice: {voice!r}"
    return None


def _validate_clips(clips: list[Clip], note: str | None) -> str | None:
    if not 1 <= len(clips) <= MAX_CLIPS:
        return f"clips must be 1-{MAX_CLIPS} items"
    for clip in clips:
        err = text_error(clip.text, MAX_CLIP_TEXT) or label_error(clip.caption, "caption")
        if err:
            return err
    return label_error(note, "note", max_len=MAX_NOTE, allow_newlines=True)


async def _dispatch(clips: list[Clip], note: str | None, voice: str | None) -> SpeakResult:
    try:
        sink = await asyncio.to_thread(make_sink)
    except ConfigError as exc:
        return SpeakResult(ok=False, sink="none", message=f"config: {exc}")

    if voice is not None:
        try:
            synth = await asyncio.to_thread(make_synth)
        except ConfigError as exc:
            return SpeakResult(ok=False, sink="none", message=f"config: {exc}")
        voice_err = _validate_voice(synth, voice)
        if voice_err:
            return SpeakResult(ok=False, sink="none", message=voice_err)

    try:
        return await asyncio.to_thread(sink.play, clips, voice, note=note)
    except ConfigError as exc:
        return SpeakResult(ok=False, sink="none", message=f"config: {exc}")


@srv.tool()
async def speak(text: str, title: str | None = None, voice: str | None = None) -> SpeakResult:
    """Say one piece of text out loud. Local: plays in the background and
    returns immediately. Cloud/Telegram: sends one voice message (caption =
    title) and returns once it is sent."""
    err = text_error(text, MAX_TEXT) or label_error(title, "title")
    if err:
        return SpeakResult(ok=False, sink="none", message=err)
    return await _dispatch([Clip(text=text, caption=title)], None, voice)


@srv.tool()
async def speak_clips(clips: list[Clip], note: str | None = None,
                       voice: str | None = None) -> SpeakResult:
    """Say a sequence of independent clips. Local: queued playback in order
    (note is ignored). Telegram: note is sent first as plain text, then one
    voice message per clip. A failure on one clip never stops the others."""
    err = _validate_clips(clips, note)
    if err:
        return SpeakResult(ok=False, sink="none", message=err)
    return await _dispatch(clips, note, voice)


@srv.tool()
async def speak_stop() -> SpeakResult:
    """Stop local playback and clear the queue. No-op (with a message) on Telegram."""
    try:
        sink = await asyncio.to_thread(make_sink)
    except ConfigError as exc:
        return SpeakResult(ok=False, sink="none", message=f"config: {exc}")
    return await asyncio.to_thread(sink.stop)
