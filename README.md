# mcps-catalog

A public catalog of **[Matheus Borges](https://github.com/MathBorgess)**'s MCP servers, installable as Claude Code plugins. Following the layout of [skills-catalog](https://github.com/MathBorgess/skills-catalog): the repo is its own marketplace.

| Plugin | What | Where it runs |
|---|---|---|
| [`speak`](speak/) | Text → speech (Kokoro-82M, pt-BR by default). Plays on the Mac; sends Telegram voice messages from cloud sessions. | macOS (local) and the Claude Code cloud VM (Ubuntu 24.04 x86_64) |
| [`ultrafast-browser`](ultrafast-browser/) | The Laya browser-use MCP (`laya-mcp`) from [ultrafast-browser-mcp](https://github.com/MathBorgess/ultrafast-browser-mcp), vendored as a git submodule to pin the version. | macOS only (depends on Apple MLX and a local Chrome) |

## Install

```bash
claude plugin marketplace add MathBorgess/mcps-catalog
claude plugin install speak@mathborgess-mcps
claude plugin install ultrafast-browser@mathborgess-mcps
```

Or, from inside a session:

```
/plugin marketplace add MathBorgess/mcps-catalog
/plugin install speak@mathborgess-mcps
/plugin install ultrafast-browser@mathborgess-mcps
```

This repo is not on Anthropic's official listing, so `claude plugin marketplace add` points straight at the GitHub repo. Update later with `claude plugin marketplace update mathborgess-mcps` and `claude plugin update <name>@mathborgess-mcps`.

macOS prerequisite for `speak`: **`brew install espeak-ng`**. The pip-bundled `espeakng-loader` has a hard-coded data path and is not used; a system espeak-ng install is what `speak` actually links against. `ultrafast-browser` needs macOS on Apple Silicon, a local Chrome reachable over remote debugging, and its own upstream `.env` — see [`ultrafast-browser/README.md`](ultrafast-browser/README.md).

## speak

Three tools: `speak(text, title?, voice?)` for one piece of text, `speak_clips(clips, note?, voice?)` for a sequence of independent clips, `speak_stop()` to stop local playback. The server's own MCP instructions tell the model to write spoken prose for the ear (no markdown, no URLs, numbers spelled out) and call `speak`/`speak_clips`, so day to day you just ask in plain language:

> "me fala em áudio o resumo dessa PR"
> "read this back to me"

Locally on the Mac it plays right away through `afplay` (or `paplay`/`aplay` on Linux). In a cloud session it sends a Telegram voice message to the owner instead — same tools, the server picks the sink from the environment.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `SPEAK_SINK` | unset → `telegram` if `CLAUDE_CODE_REMOTE_SESSION_ID`/`CLAUDE_CODE_REMOTE` is set, else `local` | `local` or `telegram`, overrides the auto-detection |
| `SPEAK_VOICE` | `pf_dora` | Kokoro voice id; its first letter picks the language (`p`→pt-br, `a`→en-us, `b`→en-gb, `e`→es, `f`→fr-fr, `i`→it, `j`→ja, `z`→cmn, `h`→hi) |
| `SPEAK_SPEED` | `1.0` | `0.7`–`1.5` |
| `SPEAK_HOME` | `/opt/speak` if that directory exists, else `~/.cache/speak-mcp` | models live in `<home>/models/` |
| `TELEGRAM_BOT_TOKEN` | — | required for the `telegram` sink |
| `TELEGRAM_CHAT_ID` | — | required for the `telegram` sink |
| `SPEAK_ESPEAK_LIB` | auto-detected | override the espeak-ng shared library path |
| `SPEAK_ESPEAK_DATA` | auto-detected | override the espeak-ng data directory |

Missing env, a missing model, or missing espeak-ng never crash the server — every tool call comes back as a normal result with `ok: false`. The Kokoro model (`kokoro-v1.0.int8.onnx` + `voices-v1.0.bin`, ~140 MB total) downloads on first use into `SPEAK_HOME/models`.

### Cloud sessions

Cloud sessions don't install plugins, so [`cloud/setup.sh`](cloud/setup.sh) provisions and registers `speak` there instead. Paste the whole file into the environment's **Settings → Environment → Setup script**, and add to that same environment:

- **`api.telegram.org`** in the allowed domains (Custom, on top of the defaults) — without it `speak` has no way to reach the Telegram API.
- The **`TELEGRAM_BOT_TOKEN`** and **`TELEGRAM_CHAT_ID`** environment variables.

The script installs `espeak-ng`, builds a venv at `/opt/speak/venv` with the `speak` package from this repo, downloads the models, and runs `claude mcp add --scope user speak ...`. If a session ever starts without that registration having survived, add a per-repo `.mcp.json` as a fallback:

```json
{
  "mcpServers": {
    "speak": {
      "command": "/opt/speak/venv/bin/speak-mcp",
      "env": { "SPEAK_HOME": "/opt/speak" }
    }
  }
}
```

## Security notes

- The `speak` tools never accept a destination, chat id, URL, or file path — where a message goes is decided entirely by `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` in the environment, never by the model or the caller.
- The Telegram token never appears in any tool result or error, including chained exceptions — it is redacted everywhere it could otherwise leak.
- `title`, `caption`, and `note` (visible Telegram text) reject `://`, `www.`, and control characters, so a tool call can't turn into a link or a terminal-escape payload in a chat message.
- This is a **public repository**. It holds no secrets, no tokens, and no personal chat IDs — those live only in each user's own environment variables.

## Contributing

See [`AGENTS.md`](AGENTS.md) for the repo layout, how to add a new MCP plugin, testing commands, and release/update steps. Changes land through pull requests against `main`; direct pushes to `main` are not the workflow here.

## License

MIT © Matheus Borges — see [`LICENSE`](LICENSE).
