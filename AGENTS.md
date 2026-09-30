# mcps-catalog — conventions

This repository is a catalog of the owner's MCP servers, packaged as Claude Code plugins. The repo is its own marketplace (`.claude-plugin/marketplace.json`, name `mathborgess-mcps`) — there is no separate publish step to PyPI or npm; a plugin install reads straight from this Git repo (or, for `ultrafast-browser`, from the pinned upstream commit via `uvx`).

Design background: [`docs/specs/2026-09-28-mcps-catalog-design.md`](docs/specs/2026-09-28-mcps-catalog-design.md).

## Layout

```
mcps-catalog/
  .claude-plugin/marketplace.json    marketplace "mathborgess-mcps"; one entry per plugin
  <plugin-name>/
    .claude-plugin/plugin.json       mcpServers.<plugin-name> -> how Claude Code launches it
    README.md                        plugin-specific usage/config (referenced from the root README)
    pyproject.toml, <pkg>/, tests/   only for plugins that ship their own Python server (e.g. speak)
    upstream/                        only for plugins that vendor another repo as a submodule
  cloud/setup.sh                     cloud-environment setup script (currently: speak only)
  docs/specs/                        design docs
  scripts/                           repo-level checks (e.g. check_pins.py)
  tests/                             tests for scripts/, run from the repo root
  AGENTS.md  CLAUDE.md  README.md  LICENSE
  .github/workflows/ci.yml
```

`AGENTS.md` is the conventions file Codex and other agents read; `CLAUDE.md` imports it (`@AGENTS.md`) so Claude Code sees the same rules. The two must not disagree — edit `AGENTS.md`.

## Adding a new MCP plugin

1. Create `<plugin-name>/.claude-plugin/plugin.json`: `name`, `version`, `description`, `author`, `homepage`/`repository` (both point at this repo), `license`, and `mcpServers.<plugin-name>` with the `command`/`args` Claude Code should launch. Two shapes exist so far:
   - **Server lives in this repo** (like `speak`): `"command": "uvx", "args": ["--from", "${CLAUDE_PLUGIN_ROOT}", "<console-script>"]`, backed by a `pyproject.toml` + package at the plugin root with `[project.scripts]` defining that console script.
   - **Server is vendored from elsewhere** (like `ultrafast-browser`): add the upstream repo as a git submodule under `<plugin-name>/upstream/` for pinning/review, but run it via `"command": "uvx", "args": ["--from", "git+https://github.com/<org>/<repo>@<full-40-char-sha>", "<entry-point>"]` — never depend on the submodule being cloned to *use* the plugin. If you add this shape, also extend `scripts/check_pins.py` (or add a sibling script) so CI fails when the pinned sha and the submodule commit drift apart, the same way it does for `ultrafast-browser`.
2. Add a matching entry to `.claude-plugin/marketplace.json` → `plugins`: `name`, `source` (`./<plugin-name>`), `description`, `category`, `keywords`.
3. Write `<plugin-name>/README.md`: what it does, requirements, configuration/env vars, how it's actually launched (the `command`/`args` above), and update steps if it vendors a submodule. Link it from the root `README.md`'s plugin table.
4. If the plugin needs anything provisioned in a Claude Code cloud session (models, system packages, a registered MCP server for the session user), add or extend a script under `cloud/` following the pattern in `cloud/setup.sh`: `set -u`, background independent steps (network installs, package installs) and only `wait` on what a later step actually depends on, download large files to `.part` and verify the size before renaming into place, keep every network call bounded by a timeout, log failures to stderr with a `<plugin-name>-setup:` prefix, and always `exit 0` — a setup script must never fail the session it's pasted into.
5. Extend `.github/workflows/ci.yml`: add the plugin's own tests to (or alongside) the `unit` job's matrix, and a `cloud-smoke`-style job if it ships a `cloud/` setup script. Keep `permissions: contents: read` at the top level, and never put a `${{ ... }}` GitHub Actions expression directly inside a `run:` block — pass it through `env:` first and reference the resulting shell variable instead, so a crafted PR title/branch/label can't inject a shell command.
6. Run the validation commands below, then open a pull request against `main`.

## Testing

```bash
# a single plugin's own suite (example: speak)
cd speak && pip install -e . pytest && pytest -q -rs

# repo-level scripts (e.g. check_pins.py) and their tests
python3 scripts/check_pins.py
python3 -m pytest -q -rs   # root pytest.ini scopes this to tests/

# laya-computer: synthetic portable tests; native UI/model validation is separate
uv run --project laya-computer pytest -q -rs laya-computer/tests

# any cloud/*.sh setup script, before it ever runs on a real VM
bash -n cloud/setup.sh
shellcheck cloud/setup.sh   # if installed; CI doesn't require it but treat a warning as a bug
```

`-rs` (short test summary for skips) matters here: several tests only run when a real model/espeak-ng is present (`SPEAK_HOME` pointing at installed weights) and otherwise skip — `-rs` is what shows that a skip happened instead of hiding it, which is how CI's `cloud-smoke` job proves the real-synthesis test actually ran unskipped rather than silently passing zero tests.

CI (`.github/workflows/ci.yml`) runs four jobs on every PR: `unit` (speak tests, matrixed across ubuntu-24.04 and macos-14), `laya-computer-unit` (synthetic controller/driver/protocol tests on the same platforms, without native model weights), `pins` (`check_pins.py` plus the root test suite, submodules checked out), and `cloud-smoke` (runs the real `cloud/setup.sh` on ubuntu-24.04 — the same OS/arch as the Claude Code cloud VM — then a real synthesis and the real-synthesis test, unskipped). `cloud-smoke` installs from **this PR's head commit**, not `main`, so a setup-script regression is caught before merge.

## Release / update

There is no version-bump-and-publish step — merging to `main` *is* the release, because plugins install straight from this Git repo:

- **Local installs**: `claude plugin marketplace update mathborgess-mcps` then `claude plugin update <plugin-name>@mathborgess-mcps` picks up whatever is on `main`.
- **Cloud sessions**: nothing auto-updates. Re-run (or re-paste) `cloud/setup.sh` in the environment's Setup script slot — it reinstalls the `speak` package from `main` (or whatever `SPEAK_REF` is set to) and re-registers the MCP server idempotently.
- **`ultrafast-browser` specifically**: bump the submodule and the pin together — `git -C ultrafast-browser/upstream fetch && git -C ultrafast-browser/upstream checkout <new-sha>` (or `git submodule update --remote ultrafast-browser/upstream`), stage the submodule bump, then update the `git+...@<sha>` arg in `ultrafast-browser/.claude-plugin/plugin.json` to the same **full 40-char sha**. Run `python3 scripts/check_pins.py` before opening the PR — it's the same check CI runs, and it rejects an abbreviated or mismatched sha.
- Bump `version` in the plugin's own `plugin.json` for a user-visible change worth surfacing (`claude plugin update` reports the new version); it is not otherwise load-bearing since there's no registry serving multiple versions.

## Contributing

Work on a branch and open a pull request against `main`; do not push directly to `main`. Keep the plugins' concerns separate — a change to `speak` shouldn't touch `ultrafast-browser`'s files or vice versa, and neither should touch `cloud/setup.sh` unless it's actually provisioning something for that plugin.
