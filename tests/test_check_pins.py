"""Tests for scripts/check_pins.py.

Run from the repo root: `python3 -m pytest -q tests/test_check_pins.py` (stdlib + pytest
only, no project deps needed — this file exercises a standalone script).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_pins.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_pins", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def cp():
    """A fresh import per test, so module-level globals (PLUGIN_JSON, SUBMODULE_DIR, ...)
    can be monkeypatched without leaking between tests."""
    return _load_module()


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


def _write_plugin_json(path: Path, sha: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "ultrafast-browser": {
                        "command": "uvx",
                        "args": [
                            "--from",
                            f"git+https://github.com/MathBorgess/ultrafast-browser-mcp@{sha}",
                            "laya-mcp",
                        ],
                    }
                }
            }
        )
    )


# ---- pinned_sha() ----


def test_pinned_sha_reads_the_pin(cp, tmp_path):
    plugin_json = tmp_path / "plugin.json"
    _write_plugin_json(plugin_json, "abc1234")
    cp.PLUGIN_JSON = plugin_json
    assert cp.pinned_sha() == "abc1234"


def test_pinned_sha_missing_file_raises(cp, tmp_path):
    cp.PLUGIN_JSON = tmp_path / "does-not-exist.json"
    with pytest.raises(cp.CheckError, match="not found"):
        cp.pinned_sha()


def test_pinned_sha_invalid_json_raises(cp, tmp_path):
    plugin_json = tmp_path / "plugin.json"
    plugin_json.write_text("{not json")
    cp.PLUGIN_JSON = plugin_json
    with pytest.raises(cp.CheckError, match="not valid JSON"):
        cp.pinned_sha()


def test_pinned_sha_missing_args_raises(cp, tmp_path):
    plugin_json = tmp_path / "plugin.json"
    plugin_json.write_text(json.dumps({"mcpServers": {}}))
    cp.PLUGIN_JSON = plugin_json
    with pytest.raises(cp.CheckError, match="no mcpServers"):
        cp.pinned_sha()


def test_pinned_sha_no_match_raises(cp, tmp_path):
    plugin_json = tmp_path / "plugin.json"
    plugin_json.write_text(
        json.dumps({"mcpServers": {"ultrafast-browser": {"args": ["--from", "laya-mcp"]}}})
    )
    cp.PLUGIN_JSON = plugin_json
    with pytest.raises(cp.CheckError, match=r"no git\+"):
        cp.pinned_sha()


# ---- submodule_sha() ----


def test_submodule_sha_from_checked_out_submodule(cp, tmp_path):
    sub = tmp_path / "sub"
    _init_repo(sub)
    (sub / "f.txt").write_text("x")
    _git("add", "f.txt", cwd=sub)
    _git("commit", "-q", "-m", "c", cwd=sub)
    expected = subprocess.run(
        ["git", "-C", str(sub), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    cp.SUBMODULE_DIR = sub
    assert cp.submodule_sha() == expected.lower()


def test_submodule_sha_falls_back_to_ls_tree_when_not_checked_out(cp, tmp_path):
    """No `.git` under upstream/ (submodule never initialized) -> read the gitlink recorded
    in the superproject's tree instead, per the brief's part (a)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = "1" * 40
    sub_path = repo / "ultrafast-browser" / "upstream"
    sub_path.mkdir(parents=True)
    # Register a gitlink (mode 160000) at that sha without an actual submodule clone.
    _git(
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{sha},ultrafast-browser/upstream",
        cwd=repo,
    )
    _git("commit", "-q", "-m", "add gitlink", cwd=repo)

    cp.REPO_ROOT = repo
    cp.SUBMODULE_DIR = sub_path  # exists on disk but has no .git -> forces the ls-tree path
    cp.SUBMODULE_PATH = "ultrafast-browser/upstream"
    assert cp.submodule_sha() == sha


def test_submodule_sha_raises_when_gitlink_missing(cp, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "f.txt").write_text("x")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-q", "-m", "c", cwd=repo)

    cp.REPO_ROOT = repo
    cp.SUBMODULE_DIR = repo / "ultrafast-browser" / "upstream"
    cp.SUBMODULE_PATH = "ultrafast-browser/upstream"
    with pytest.raises(cp.CheckError):
        cp.submodule_sha()


# ---- check() / main() ----


def test_check_passes_on_match(cp, monkeypatch):
    monkeypatch.setattr(cp, "pinned_sha", lambda: "deadbeef")
    monkeypatch.setattr(cp, "submodule_sha", lambda: "deadbeef")
    cp.check()  # must not raise


def test_check_raises_on_mismatch(cp, monkeypatch):
    monkeypatch.setattr(cp, "pinned_sha", lambda: "aaaa")
    monkeypatch.setattr(cp, "submodule_sha", lambda: "bbbb")
    with pytest.raises(cp.CheckError, match="pin mismatch"):
        cp.check()


def test_main_returns_0_on_match(cp, monkeypatch):
    monkeypatch.setattr(cp, "pinned_sha", lambda: "same")
    monkeypatch.setattr(cp, "submodule_sha", lambda: "same")
    assert cp.main() == 0


def test_main_returns_1_on_mismatch(cp, monkeypatch, capsys):
    monkeypatch.setattr(cp, "pinned_sha", lambda: "aaaa")
    monkeypatch.setattr(cp, "submodule_sha", lambda: "bbbb")
    assert cp.main() == 1
    assert "FAIL" in capsys.readouterr().err


def test_script_against_the_real_repo():
    """End-to-end: run the actual script as a subprocess against this real checkout, where
    the submodule pin should always be in sync on main/PR branches."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
