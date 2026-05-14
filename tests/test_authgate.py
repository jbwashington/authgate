"""Round-trip tests using a temp HOME so we don't touch real auth state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_authgate(env_home: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(env_home)
    # Force the module under test (in case authgate is also installed globally)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "authgate", *args],
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway HOME with a fake stripe config to operate on."""
    stripe_dir = tmp_path / ".config" / "stripe"
    stripe_dir.mkdir(parents=True)
    cfg = stripe_dir / "config.toml"
    cfg.write_text("project-name = 'default'\n[default]\naccount_id = 'acct_AAA'\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_services_command(fake_home: Path):
    r = run_authgate(fake_home, "services")
    assert r.returncode == 0, r.stderr
    assert "stripe" in r.stdout
    assert "cf" in r.stdout


def test_list_empty(fake_home: Path):
    r = run_authgate(fake_home, "list")
    assert r.returncode == 0, r.stderr


def test_add_and_use_roundtrip(fake_home: Path):
    cfg = fake_home / ".config" / "stripe" / "config.toml"
    original = cfg.read_text()

    r = run_authgate(fake_home, "stripe", "add", "first")
    assert r.returncode == 0, r.stderr
    assert "first" in r.stdout

    # State file should mark 'first' as active
    state = json.loads((fake_home / ".authgate" / "state.json").read_text())
    assert state["stripe"] == "first"

    # Mutate live state, then snapshot as 'second'
    cfg.write_text("project-name = 'default'\n[default]\naccount_id = 'acct_BBB'\n")
    r = run_authgate(fake_home, "stripe", "add", "second")
    assert r.returncode == 0, r.stderr

    # Switch back to first
    r = run_authgate(fake_home, "stripe", "use", "first")
    assert r.returncode == 0, r.stderr
    assert cfg.read_text() == original

    # And back to second
    r = run_authgate(fake_home, "stripe", "use", "second")
    assert r.returncode == 0, r.stderr
    assert "BBB" in cfg.read_text()


def test_use_refuses_unsnapshotted_state(fake_home: Path):
    # Live state exists from fixture, but no profile is marked active.
    # Trying to use a profile without snapshotting first should fail.
    r = run_authgate(fake_home, "stripe", "use", "nonexistent")
    assert r.returncode != 0


def test_rm_clears_active(fake_home: Path):
    run_authgate(fake_home, "stripe", "add", "first")
    r = run_authgate(fake_home, "stripe", "rm", "first", "--force")
    assert r.returncode == 0, r.stderr
    state = json.loads((fake_home / ".authgate" / "state.json").read_text())
    assert "stripe" not in state
