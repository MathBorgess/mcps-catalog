# speak

Text-to-speech MCP server: [Kokoro-82M](https://github.com/thewh1teagle/kokoro-onnx) TTS. Plays locally through `afplay`/`paplay`/`aplay` on a Mac or Linux box, or sends a Telegram voice message when running in a Claude Code cloud session — same three tools, the server picks the sink from the environment. See the [repo README](../README.md) for install/marketplace instructions and the [design spec](../docs/specs/2026-09-28-mcps-catalog-design.md) for the full design.

## macOS prerequisites

```bash
brew install espeak-ng
brew install uv
```

- **`espeak-ng`**: Kokoro needs a *system* espeak-ng install for phonemization. The pip-bundled `espeakng-loader` has a hard-coded data path and is not used; `speak_mcp.synth.resolve_espeak` looks for the Homebrew/Linux library and data directory (or `SPEAK_ESPEAK_LIB`/`SPEAK_ESPEAK_DATA` overrides).
- **`uv`**: the plugin runs as `uvx --from ${CLAUDE_PLUGIN_ROOT} speak-mcp` ([`.claude-plugin/plugin.json`](.claude-plugin/plugin.json)); `uv`/`uvx` must be on `PATH` for Claude Code to launch it.

On Linux (including the cloud VM) the equivalent is `apt install espeak-ng`; `uv` is what `cloud/setup.sh` installs and uses there.

## Tools

| Tool | Args | Behavior |
|---|---|---|
| `speak` | `text` (1–12000 chars), `title?` (≤300 chars), `voice?` | Say one piece of text. Local: plays in the background, returns immediately. Telegram: sends one voice message (caption = `title`), returns once sent, or `queued: true` if that takes longer than `SPEAK_WAIT_SECONDS` (see Sinks). |
| `speak_clips` | `clips: [{text, caption?}]` (1–20 clips, each `text` 1–3000 chars, `caption?` ≤300 chars), `note?` (≤3000 chars), `voice?` | Say a sequence of independent clips. Local: queued playback in order (`note` ignored). Telegram: `note` sent first as plain text, then one voice message per clip; a failure on one clip never stops the others. If delivery outlasts `SPEAK_WAIT_SECONDS` the call answers `queued: true` (see Sinks). |
| `speak_stop` | — | Local: stop current playback and clear the queue. Telegram: no-op with a message. |

`text` must not be blank (whitespace-only is rejected) and is never link-checked (it's spoken, not shown). `title`, `caption` and `note` are visible Telegram text: they reject control characters and `://`/`www.` (not a full link blocker — Telegram still auto-links bare domains and `@handles` on its own). None of the tools accept a destination, chat id, URL, or file path; where a message goes is decided entirely by `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` in the environment.

`voice`: a Kokoro voice id from the loaded voices file. The language comes from the id's first letter: `p`→pt-br, `a`→en-us, `b`→en-gb, `e`→es, `f`→fr-fr, `i`→it, `j`→ja, `z`→cmn, `h`→hi. An explicit `voice` override, and the default voice (`SPEAK_VOICE`) when none is given, are both validated against the loaded voices before dispatch.

Missing env, a missing model, or missing espeak-ng never crash the server — every tool call comes back as a normal result with `ok: false` and a `message`.

## Sinks

`SPEAK_SINK` picks `local` or `telegram`; unset, it auto-detects `telegram` when `CLAUDE_CODE_REMOTE_SESSION_ID`/`CLAUDE_CODE_REMOTE` is set (a cloud session), else `local`.

- **local**: text is chunked into ≤~400-char sentence pieces (never mid-word); a single background worker synthesizes and plays chunk by chunk, pipelined one chunk ahead, so long text starts within seconds. The player binary (`afplay`/`paplay`/`aplay`) is resolved once, up front, before anything is queued — if none is found, the call returns `ok: false` instead of a silent no-op. `speak_stop`, and server shutdown (normal exit or `SIGTERM`), stop the current player and drain the queue.
- **telegram**: each clip's full text is chunked, synthesized and concatenated into one OGG/Opus voice message (`sendVoice`, 24kHz), caption as plain text, no `parse_mode`, link previews disabled on the `note` message. A partial failure across several clips is reported in the top-level `message` as `"<failed>/<total> clips failed"`.

  **Soft deadline.** Synthesis on the cloud VM runs slower than real time (about 50 s per 30 s clip), so a batch like a radar edition takes minutes, longer than the 60 s Claude Code gives an MCP tool call before it drops the reply. A single worker delivers jobs in call order; a call waits for its own job for up to `SPEAK_WAIT_SECONDS` (default 45). Finished in time: the usual full per-clip report. Not finished: the call answers `ok: true, queued: true` with the clips delivered so far and the rest marked `pending: true`, and delivery continues in the background. `message` then reads e.g. `"1/3 clips sent, 2 still sending in the background (their results are not reported)"`. **Do not resend on `queued: true`** — that would deliver duplicates. A pending clip's `ok: true` only means "accepted"; if it fails later, the error goes to stderr only. On a normal shutdown (stdin closed) the server waits up to 5 minutes for accepted clips to finish; on `SIGTERM` it exits at once. The bot token never appears in any tool result, error, or log line (httpx's and httpcore's own loggers are held at `WARNING`).

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `SPEAK_SINK` | unset → auto-detect | `local` or `telegram`, overrides the auto-detection |
| `SPEAK_VOICE` | `pf_dora` | default Kokoro voice id |
| `SPEAK_SPEED` | `1.0` | `0.7`–`1.5` |
| `SPEAK_HOME` | `/opt/speak` if that directory exists, else `~/.cache/speak-mcp` | models live in `<home>/models/` |
| `SPEAK_WAIT_SECONDS` | `45` | `telegram` sink only: how long a call waits for delivery before answering `queued: true` (`0`–`3600`; keep it under the client's 60s tool timeout) |
| `TELEGRAM_BOT_TOKEN` | — | required for the `telegram` sink |
| `TELEGRAM_CHAT_ID` | — | required for the `telegram` sink |
| `SPEAK_ESPEAK_LIB` | auto-detected | override the espeak-ng shared library path |
| `SPEAK_ESPEAK_DATA` | auto-detected | override the espeak-ng data directory |

## Models

Kokoro `kokoro-v1.0.int8.onnx` (114119327 bytes) and `voices-v1.0.bin` (28214398 bytes), ~140MB total, from the `model-files-v1.1` release of [thewh1teagle/kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx). Downloaded on first use into `SPEAK_HOME/models` (to `.part`, size-checked, then renamed into place; a dropped connection retries with backoff). `speak-mcp --setup` downloads them and warms the synth up front without starting the server.

## CLI

```
speak-mcp                          # run the stdio MCP server (default)
speak-mcp --say "texto" [--voice v]  # synthesize and play locally, blocking (manual smoke test)
speak-mcp --sample "texto" --out f.ogg [--voice v]  # synthesize to a file, no playback
speak-mcp --setup                  # download models and warm the synth, then exit
```

## Testing

```bash
cd speak && pip install -e . pytest && pytest -q -rs
```

`tests/test_real_synthesis_pt_br` (in `test_synth.py`) and the F4 stdio/real-model-load test (in `test_server.py`) are skipped unless a Kokoro model is present under `SPEAK_HOME` (or `/opt/speak` by default) — CI's `cloud-smoke` job runs the full suite against a real provisioned venv so those run unskipped there. No test ever invokes `--say`, plays through a real player, or calls a live Telegram API; network and audio players are always faked or mocked.
