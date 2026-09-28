# ultrafast-browser

Wraps the [Laya](https://github.com/mizorewww/laya-mlx)-powered browser-use MCP server from
[MathBorgess/ultrafast-browser-mcp](https://github.com/MathBorgess/ultrafast-browser-mcp) (`laya-mcp` entry
point) as a Claude Code plugin. No server code lives in this repo — the upstream project is vendored as a git
submodule purely to pin the version; the plugin runs it via `uvx` straight from the upstream Git URL.

## Requirements

- **macOS on Apple Silicon.** Laya's local decisions run through [laya-mlx](https://github.com/mizorewww/laya-mlx),
  which needs an M-series Mac and macOS 14+. There is no Linux or Intel build.
- **Local Chrome**, reachable over remote debugging (`chrome://inspect/#remote-debugging`) — the upstream server
  connects to it through [Browser Harness](https://github.com/browser-use/browser-harness).
- `uv`/`uvx` on `PATH` (same as the `speak` plugin).

## Configuration

All configuration is the upstream server's own `.env` — this repo never copies or restates it. Read
[the upstream README](https://github.com/MathBorgess/ultrafast-browser-mcp#configuration) and
[`.env.example`](https://github.com/MathBorgess/ultrafast-browser-mcp/blob/main/.env.example) for the full variable
list (decision model, text-model endpoint/key, etc.) and set them in your own shell/environment before launching
Claude Code — this plugin does not manage secrets and none are stored in this public catalog.

## How it runs

`ultrafast-browser/.claude-plugin/plugin.json` registers `mcpServers.ultrafast-browser` as:

```
uvx --from git+https://github.com/MathBorgess/ultrafast-browser-mcp@<pinned sha> laya-mcp
```

`<pinned sha>` is the exact commit `ultrafast-browser/upstream` (the submodule) points at — `uvx` builds and runs
the upstream package straight from that Git ref, so cloning the submodule is never required to *use* the plugin;
`upstream/` exists only so the pinned commit is reviewable and diffable in this repo.

## Updating

1. `git -C ultrafast-browser/upstream fetch && git -C ultrafast-browser/upstream checkout <new sha>` (or `git
   submodule update --remote ultrafast-browser/upstream`), then `git add ultrafast-browser/upstream`.
2. Update the sha in the `uvx --from git+...@<sha>` arg in `ultrafast-browser/.claude-plugin/plugin.json` to match.
3. Run `python3 scripts/check_pins.py` — it fails if the two shas disagree. CI runs the same check on every PR.
