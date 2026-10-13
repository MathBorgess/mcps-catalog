# mcps-catalog

A public catalog of **[Matheus Borges](https://github.com/MathBorgess)**'s MCP servers, installable as Claude Code plugins. Following the layout of [skills-catalog](https://github.com/MathBorgess/skills-catalog): the repo is its own marketplace.

| Plugin | What | Where it runs |
|---|---|---|
| [`speak`](speak/) | Text → speech (Qwen3-TTS 1.7B MLX 4-bit, Portuguese by default). Local playback or Telegram from the Mac. | macOS Apple Silicon |
| [`ultrafast-browser`](ultrafast-browser/) | The Laya browser-use MCP (`laya-mcp`) from [ultrafast-browser-mcp](https://github.com/MathBorgess/ultrafast-browser-mcp), vendored as a git submodule to pin the version. | macOS only (depends on Apple MLX and a local Chrome) |
| [`laya-computer`](laya-computer/) | Execute structured desktop plans with local Laya decisions and Cua Driver; caller-owned rescue. | macOS on Apple Silicon; Cua Driver required |

## Install

```bash
claude plugin marketplace add MathBorgess/mcps-catalog
claude plugin install speak@mathborgess-mcps
claude plugin install ultrafast-browser@mathborgess-mcps
claude plugin install laya-computer@mathborgess-mcps
```

Or, from inside a session:

```
/plugin marketplace add MathBorgess/mcps-catalog
/plugin install speak@mathborgess-mcps
/plugin install ultrafast-browser@mathborgess-mcps
/plugin install laya-computer@mathborgess-mcps
```

This repo is not on Anthropic's official listing, so `claude plugin marketplace add` points straight at the GitHub repo. Update later with `claude plugin marketplace update mathborgess-mcps` and `claude plugin update <name>@mathborgess-mcps`.

Prerequisites: **`uv`** on PATH. `speak` uses MLX and requires macOS Apple Silicon; no espeak-ng or ONNX runtime is needed. `ultrafast-browser` needs macOS on Apple Silicon, Chrome remote debugging, and its upstream `.env` — see its README.

## speak

See [`speak/README.md`](speak/README.md) for tools, configuration, the pinned model, and testing. The tools remain `speak(text, title?, voice?)`, `speak_clips(clips, note?, voice?)`, and `speak_stop()`. Write spoken prose, then request audio in plain language. Portuguese is the default; `SPEAK_LANGUAGE` selects language independently of the speaker timbre.

The previous Linux cloud setup is retired because MLX requires Apple Silicon. [`cloud/setup.sh`](cloud/setup.sh) is now a non-mutating explanatory no-op. Existing Linux deployments are not automatically upgraded. Telegram delivery remains available when the server runs on a Mac with `SPEAK_SINK=telegram` and its credentials supplied through the environment.

## Security notes

- The `speak` tools never accept a destination, chat id, URL, or file path — where a message goes is decided entirely by `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` in the environment, never by the model or the caller.
- The Telegram token never appears in any tool result or error, including chained exceptions — it is redacted everywhere it could otherwise leak.
- `title`, `caption`, and `note` (visible Telegram text) reject `://`, `www.`, and control characters, so a tool call can't smuggle a terminal-escape payload or an obvious `https://`/`www.`-style link into a chat message. This is not a full link blocker: Telegram auto-links bare domains (`evil.example`) and `@handles` on its own, and neither is caught by this check.
- This is a **public repository**. It holds no secrets, no tokens, and no personal chat IDs — those live only in each user's own environment variables.

## Contributing

See [`AGENTS.md`](AGENTS.md) for the repo layout, how to add a new MCP plugin, testing commands, and release/update steps. Changes land through pull requests against `main`; direct pushes to `main` are not the workflow here.

## License

MIT © Matheus Borges — see [`LICENSE`](LICENSE).
