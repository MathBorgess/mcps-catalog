# mcps-catalog — design

Status: approved by the owner on 2026-09-28. Origin: the RADAR audio work in the owner's private vault (PR MathBorgess/mathai-wiki#153), generalized so any Claude Code session — local or cloud — can "say it in audio", and so the owner's MCP servers live in one public catalog.

## 1. Goal

A public monorepo of the owner's MCP servers, installable as Claude Code plugins, following the layout of [skills-catalog](https://github.com/MathBorgess/skills-catalog): the repo is its own marketplace.

| Plugin | What | Where it runs |
|---|---|---|
| `speak` | Text → speech (Kokoro-82M, pt-BR by default). Plays on the Mac; sends Telegram voice messages from cloud sessions | macOS (local) and the Claude Code cloud VM (Ubuntu 24.04 x86_64) |
| `ultrafast-browser` | The Laya browser-use MCP (`laya-mcp`) from [ultrafast-browser-mcp](https://github.com/MathBorgess/ultrafast-browser-mcp), vendored as a git submodule | macOS only (depends on Apple MLX and a local Chrome) |

Non-goals: publishing to PyPI; a Linux build of Laya; any RADAR-specific logic (that lives as a skill in the owner's vault).

## 2. Layout

```
mcps-catalog/
  .claude-plugin/marketplace.json        marketplace "mathborgess-mcps"; plugins speak, ultrafast-browser
  speak/
    .claude-plugin/plugin.json           mcpServers.speak → uvx --from ${CLAUDE_PLUGIN_ROOT} speak-mcp
    pyproject.toml                       package speak-mcp, script speak-mcp
    speak_mcp/                           errors, synth, telegram, clips, sinks, server
    tests/
  ultrafast-browser/
    .claude-plugin/plugin.json           mcpServers.ultrafast-browser → uvx --from git+…@<submodule sha> laya-mcp
    upstream/                            git submodule → MathBorgess/ultrafast-browser-mcp
  cloud/setup.sh                         cloud environment setup script (speak only)
  docs/specs/                            this file
  AGENTS.md  CLAUDE.md  README.md  LICENSE (MIT)
  .github/workflows/ci.yml
```

Marketplace name `mathborgess-mcps` (`mathborgess` is taken by skills-catalog). Local install:

```
claude plugin marketplace add MathBorgess/mcps-catalog
claude plugin install speak@mathborgess-mcps
claude plugin install ultrafast-browser@mathborgess-mcps
```

Cloud sessions do not install plugins, so `cloud/setup.sh` (pasted into the environment's Setup script) provisions and registers `speak` there.

## 3. speak

### Tools

- `speak(text, title?, voice?)` — say one text. Local: returns immediately and plays in the background. Cloud/Telegram: sends one voice message (caption = `title`) and returns after sending.
- `speak_clips(clips: [{text, caption?}], note?, voice?)` — say a sequence. Local: queued playback in order (`note` is ignored). Telegram: `note` is sent first as a plain-text message, then one voice message per clip with its caption. Per-clip failures never stop the others; returns per-clip status.
- `speak_stop()` — local only: stop current playback and clear the queue. Telegram: no-op with a message.

Limits: `text` 1–12000 chars (`speak`), each clip 1–3000 chars, 1–20 clips, `title`/`caption` ≤ 300 chars, `note` ≤ 3000 chars. `title`, `caption` and `note` reject `://`, `www.` and control characters (they are visible Telegram text). `text` is spoken, not shown, so it is not link-checked.

`voice`: a Kokoro voice id from the loaded voices file; the language comes from the id's first letter (`p` → `pt-br`, `a` → `en-us`, `b` → `en-gb`, `e` → `es`, `f` → `fr-fr`, `i` → `it`, `j` → `ja`, `z` → `cmn`, `h` → `hi`). Default `SPEAK_VOICE` or `pf_dora`. Speed `SPEAK_SPEED` (0.7–1.5, default 1.0).

The tools never accept a destination, chat id, URL or file path.

### Sinks

`SPEAK_SINK` = `local` | `telegram` | unset. Unset → `telegram` when `CLAUDE_CODE_REMOTE_SESSION_ID` (or `CLAUDE_CODE_REMOTE`) is set, else `local`.

- **local**: text is split into sentence chunks (≤ ~400 chars, never mid-word); a single background worker synthesizes chunk by chunk and plays each with `afplay` (macOS; `paplay`/`aplay` fallback on Linux) as soon as it is ready, so long text starts within seconds. `speak_stop` kills the current player and drains the queue. Temporary files go to a private temp dir and are deleted after playing.
- **telegram**: synchronous; whole text → one OGG/Opus voice message (`sendVoice`, 24 kHz), caption plain text, no `parse_mode`, link previews disabled on `sendMessage`. Destination only from `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (stripped). The token never appears in any error, including chained exceptions.
  > Amended 2026-09-29: delivery is no longer strictly synchronous. A call waits `SPEAK_WAIT_SECONDS` (default 45) for its clips and then answers `queued: true` while delivery continues in the background, because a batch takes minutes and Claude Code drops an MCP reply after 60 s. See `speak/README.md`, section Sinks.

Config errors (missing env, model, espeak-ng) come back as a tool result with `ok: false`, never an unhandled exception. The server starts without models or env (lazy load on first call).

### Models and espeak-ng

Kokoro `kokoro-v1.0.int8.onnx` (114119327 bytes) and `voices-v1.0.bin` (28214398 bytes) from release `model-files-v1.1` of thewh1teagle/kokoro-onnx. Model dir: `SPEAK_HOME/models`, where `SPEAK_HOME` defaults to `/opt/speak` if it exists, else `~/.cache/speak-mcp`. Missing models are downloaded on first use (to `.part`, size-checked, then renamed). System espeak-ng is required (`brew install espeak-ng` / `apt install espeak-ng`); the pip-bundled espeakng-loader has a hard-coded data path and is not used.

### Server instructions

The MCP `instructions` tell the model: when the user asks to hear, listen to, or have something described/said in audio, write it as spoken prose for the ear (no markdown, no URLs, no tables; numbers and acronyms as they are spoken; the user's language) and call `speak`, or `speak_clips` for several independent parts.

## 4. ultrafast-browser

No new code. The plugin runs the upstream `laya-mcp` entry point, installed by `uvx` from the upstream Git URL pinned to the submodule's commit, so the plugin never depends on cloning submodules. Updating = bump the submodule and the pinned sha together (CI checks they match). Laya's own configuration (its `.env`: model endpoint and keys) stays as documented upstream.

## 5. Cloud

`cloud/setup.sh` (root, before Claude Code launches, must exit 0, < ~5 min to be cached): apt `espeak-ng`; venv at `/opt/speak/venv` with the speak package from this repo (pinned deps); models into `/opt/speak/models`; then registers the server for the session user with `claude mcp add --scope user` (the cloud probe of 2026-09-28 showed: sessions run as root with HOME=/root, `claude` on PATH at /opt/node22/bin, `claude mcp add --scope user` writes /root/.claude.json, `CLAUDE_CODE_REMOTE=true` and `CLAUDE_CODE_REMOTE_SESSION_ID` are set, system Python is 3.11.15, uv 0.8.17, no audio player). Whether a registration made at setup time survives session start is verified in the first end-to-end run; fallback: a per-repo `.mcp.json` pointing to `/opt/speak/venv/bin/speak-mcp`. The package supports Python ≥ 3.11. The environment needs `api.telegram.org` in its allowed domains and the two `TELEGRAM_*` variables.

## 6. RADAR (consumer, in the owner's vault)

The RADAR routine's audio step becomes a project skill `.claude/skills/radar-audio/` in the vault that writes one spoken script per item and calls `speak_clips` with the edition index as `note` and captions `n. headline · score · slug`, hooks first. The vault no longer carries TTS code.

## 7. Testing

Unit tests (no network, no model, no espeak-ng): clip validation, voice → language, sink selection, sentence chunking, local player queue/stop with a fake player, Telegram client with httpx MockTransport, server tools with fake synth/sinks. CI: unit on ubuntu-24.04 and macOS; a smoke job on ubuntu-24.04 runs `cloud/setup.sh` and a real synthesis; a check that the ultrafast-browser plugin's pinned sha equals the submodule commit.
