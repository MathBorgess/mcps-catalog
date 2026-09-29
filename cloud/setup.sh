#!/bin/bash
# speak cloud environment setup script.
#
# Paste this whole file into claude.ai/code -> the environment's Settings -> Environment ->
# Setup script. It runs as root, before Claude Code launches and before this repo is guaranteed
# to exist, so it is self-contained and never reads anything else from the checkout. It also
# needs, on that same environment:
#   - api.telegram.org added to the allowed domains (Custom, on top of the defaults) -- without
#     it the speak MCP has no way to reach the Telegram API for voice messages.
#   - the TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables set.
#     TELEGRAM_CHAT_ID is the numeric id of YOUR private chat with the bot (the same number as
#     your Telegram user id, e.g. 123456789). It is NOT the number before the ":" in the bot
#     token (that is the bot's own id: Telegram answers "the bot can't send messages to the bot").
# Without those two, speak still installs and registers, but every call returns ok: false.
#
# Installs from git+https://github.com/MathBorgess/mcps-catalog@${SPEAK_REF:-main}: SPEAK_REF
# defaults to `main`, so pasted as-is this only works once a change has actually been merged
# there. To try an unmerged branch or PR, set the SPEAK_REF environment variable on that same
# environment (alongside TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID) to that branch name or commit.
#
# Never fails the session: every step only logs to stderr with a "speak-setup:" prefix, and
# the script always exits 0.
set -u

# The setup script can run with a minimal PATH. `uv` lives at /root/.local/bin on the Claude
# Code cloud VM and `claude` at /opt/node22/bin -- without this, `command -v uv` below can
# silently miss an already-installed uv and take the slower python3-venv fallback instead.
export PATH="/root/.local/bin:/opt/node22/bin:/usr/local/bin:$PATH"
export DEBIAN_FRONTEND=noninteractive

SPEAK_REF="${SPEAK_REF:-main}"
HOME_DIR=/opt/speak
VENV="$HOME_DIR/venv"
PKG="speak-mcp @ git+https://github.com/MathBorgess/mcps-catalog@${SPEAK_REF}#subdirectory=speak"
export UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-60}"

mkdir -p "$HOME_DIR/models"

# espeak-ng is the system phonemizer Kokoro needs: without it every speak call fails with
# "config: system espeak-ng not found" before a single clip is synthesized. python3-venv is
# only needed by the no-uv fallback below.
APT_PKGS="espeak-ng"
command -v uv >/dev/null || APT_PKGS="$APT_PKGS python3-venv"

# One attempt: `update` is best-effort on purpose. The cloud proxy answers 403 for some
# third-party PPAs already configured on the image (deadsnakes, ondrej); that must not skip
# the install, so it is never chained to it with &&. The lock timeout covers another apt/dpkg
# process still running while the VM boots.
apt_install_once() {
  apt-get -o Acquire::http::Timeout=30 -o Acquire::Retries=3 -o DPkg::Lock::Timeout=120 \
    update -qq >/dev/null 2>&1 \
    || echo "speak-setup: apt-get update reported errors, installing anyway" >&2
  # shellcheck disable=SC2086  # APT_PKGS is a space-separated list on purpose
  apt-get -o Acquire::http::Timeout=30 -o Acquire::Retries=3 -o DPkg::Lock::Timeout=120 \
    install -y -qq $APT_PKGS >/dev/null
}

# Independent of the venv/package install job, so it runs in the background and the two race.
# Retried once: a transient failure here is what leaves the VM without espeak-ng.
( apt_install_once || { sleep 5; apt_install_once; } ) \
  || echo "speak-setup: apt install of $APT_PKGS failed twice" >&2 &
APT_PID=$!

if command -v uv >/dev/null; then
  # uv manages its own venv/pip and does not need the apt job above, so it stays backgrounded
  # too -- this is the expected path on the Claude Code cloud VM (uv is preinstalled there).
  ( uv venv -q --python 3.11 "$VENV" 2>/dev/null || uv venv -q "$VENV"
    uv pip install -q --python "$VENV/bin/python" "$PKG" ) \
    || echo "speak-setup: venv/package install (uv) failed" >&2 &
else
  # python3 -m venv needs the python3-venv apt package installed by the job above. A background
  # subshell can't wait on a sibling job (only on its own children), so this branch waits here,
  # in the main shell (the apt job's actual parent), then runs in the foreground.
  wait "$APT_PID"
  ( python3 -m venv "$VENV" && "$VENV/bin/pip" install -q "$PKG" ) \
    || echo "speak-setup: venv/package install (python3 -m venv) failed" >&2
fi

wait

# Fail loud, in the setup log, when the phonemizer is still missing: this is the line to look
# for when a session's speak call returns "system espeak-ng not found". It checks the shared
# library, which is what speak_mcp.synth.resolve_espeak looks up, not the espeak-ng binary.
if ldconfig -p 2>/dev/null | grep -q 'libespeak-ng'; then
  echo "speak-setup: libespeak-ng ok" >&2
else
  echo "speak-setup: ERROR libespeak-ng still missing after install; speak will fail" >&2
fi

if [ -x "$VENV/bin/speak-mcp" ]; then
  # Downloads the Kokoro model + voices (to .part, size-checked, then renamed -- see
  # speak_mcp.synth.ensure_models) and warms the synth, which also validates system espeak-ng
  # is resolvable; needs both the apt job and the venv/package install above to be done.
  SPEAK_HOME="$HOME_DIR" "$VENV/bin/speak-mcp" --setup \
    || echo "speak-setup: model download / synth warm-up failed" >&2
else
  echo "speak-setup: venv install failed, speak-mcp not found at $VENV/bin/speak-mcp" >&2
fi

if command -v claude >/dev/null; then
  # Idempotent: drop any stale registration first (e.g. from a previous setup-script run)
  # before re-adding, since whether a user-scope registration survives session start is
  # unverified -- this must be safe to run again every time the session boots.
  claude mcp remove --scope user speak >/dev/null 2>&1 || true
  claude mcp add --scope user speak -e SPEAK_HOME="$HOME_DIR" -e SPEAK_SINK=telegram \
    -- "$VENV/bin/speak-mcp" \
    || echo "speak-setup: claude mcp add failed" >&2
else
  echo "speak-setup: claude not on PATH, skipping MCP registration" >&2
fi

ls -la "$HOME_DIR/models" || true
exit 0
