#!/usr/bin/env python3
"""Check that the ultrafast-browser plugin's pinned sha matches the submodule commit.

`ultrafast-browser/.claude-plugin/plugin.json` hardcodes a commit sha in its
`mcpServers.ultrafast-browser.args` (the `git+https://.../ultrafast-browser-mcp@<sha>` ref
passed to `uvx`). That sha must always equal the commit the `ultrafast-browser/upstream`
git submodule points at, so updating one without the other is a CI failure rather than a
silent drift.

Stdlib only. Usage: `python3 scripts/check_pins.py` (run from anywhere; it locates the repo
root from this file's own path). Exits 0 and prints a confirmation on match, exits 1 with a
clear message otherwise.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_JSON = REPO_ROOT / "ultrafast-browser" / ".claude-plugin" / "plugin.json"
SUBMODULE_PATH = "ultrafast-browser/upstream"
SUBMODULE_DIR = REPO_ROOT / SUBMODULE_PATH

# Matches "git+https://github.com/MathBorgess/ultrafast-browser-mcp@<sha>" and captures <sha>.
PIN_RE = re.compile(r"git\+https://github\.com/MathBorgess/ultrafast-browser-mcp@([0-9a-fA-F]{7,40})")


class CheckError(Exception):
    """Raised for any failure that should abort the check with a clear message."""


def pinned_sha() -> str:
    """The sha embedded in plugin.json's uvx --from arg."""
    if not PLUGIN_JSON.exists():
        raise CheckError(f"plugin manifest not found: {PLUGIN_JSON}")

    try:
        data = json.loads(PLUGIN_JSON.read_text())
    except json.JSONDecodeError as exc:
        raise CheckError(f"{PLUGIN_JSON} is not valid JSON: {exc}") from exc

    try:
        args = data["mcpServers"]["ultrafast-browser"]["args"]
    except (KeyError, TypeError) as exc:
        raise CheckError(
            f"{PLUGIN_JSON} has no mcpServers.ultrafast-browser.args to read the pin from"
        ) from exc

    for arg in args:
        match = PIN_RE.search(str(arg))
        if match:
            return match.group(1).lower()

    raise CheckError(
        f"{PLUGIN_JSON} mcpServers.ultrafast-browser.args has no "
        "git+https://github.com/MathBorgess/ultrafast-browser-mcp@<sha> pin"
    )


def submodule_sha() -> str:
    """The commit ultrafast-browser/upstream points at.

    Prefers the checked-out submodule's own HEAD; falls back to the gitlink recorded in the
    superproject's tree (`git ls-tree`) when the submodule was never initialized/checked out.
    """
    if (SUBMODULE_DIR / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "-C", str(SUBMODULE_DIR), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise CheckError(f"`git -C {SUBMODULE_DIR} rev-parse HEAD` failed: {exc}") from exc
        sha = out.stdout.strip()
        if sha:
            return sha.lower()

    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-tree", "HEAD", SUBMODULE_PATH],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise CheckError(
            f"`git -C {REPO_ROOT} ls-tree HEAD {SUBMODULE_PATH}` failed: {exc}"
        ) from exc

    line = out.stdout.strip()
    if not line:
        raise CheckError(
            f"`git ls-tree HEAD {SUBMODULE_PATH}` returned nothing — is the submodule "
            "committed in the superproject?"
        )

    # Format: "<mode> commit <sha>\t<path>"
    parts = line.split()
    if len(parts) < 3 or parts[1] != "commit":
        raise CheckError(f"unexpected `git ls-tree` output for {SUBMODULE_PATH}: {line!r}")

    return parts[2].lower()


def check() -> None:
    pinned = pinned_sha()
    actual = submodule_sha()
    if pinned != actual:
        raise CheckError(
            "pin mismatch: ultrafast-browser/.claude-plugin/plugin.json pins "
            f"{pinned}, but {SUBMODULE_PATH} is at {actual}. Bump the submodule and the "
            "plugin.json sha together."
        )
    print(f"check_pins: OK — ultrafast-browser pinned sha matches submodule commit ({pinned}).")


def main() -> int:
    try:
        check()
    except CheckError as exc:
        print(f"check_pins: FAIL — {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
