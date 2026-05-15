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
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "authgate", *args],
        env=env,
        capture_output=True,
        text=True,
    )


def _seed_stripe(home: Path, account: str) -> Path:
    d = home / ".config" / "stripe"
    d.mkdir(parents=True, exist_ok=True)
    cfg = d / "config.toml"
    cfg.write_text(f"project-name = 'default'\n[default]\naccount_id = 'acct_{account}'\n")
    return cfg


def _seed_gh(home: Path, account: str) -> Path:
    d = home / ".config" / "gh"
    d.mkdir(parents=True, exist_ok=True)
    hosts = d / "hosts.yml"
    hosts.write_text(f"github.com:\n    user: {account}\n    oauth_token: ghp_{account}_token\n")
    (d / "config.yml").write_text("# gh config\n")
    return hosts


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _seed_stripe(tmp_path, "AAA")
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# --- existing single-service behavior --------------------------------------


def test_services_command(fake_home: Path):
    r = run_authgate(fake_home, "services")
    assert r.returncode == 0, r.stderr
    assert "stripe" in r.stdout
    assert "cf" in r.stdout
    assert "supabase" in r.stdout


def test_aws_two_file_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    aws = tmp_path / ".aws"
    aws.mkdir()
    (aws / "config").write_text("[default]\nregion = us-east-1\n")
    (aws / "credentials").write_text("[default]\naws_access_key_id = AKIA_first\n")
    monkeypatch.setenv("HOME", str(tmp_path))

    r = run_authgate(tmp_path, "aws", "add", "first")
    assert r.returncode == 0, r.stderr

    (aws / "credentials").write_text("[default]\naws_access_key_id = AKIA_second\n")
    (aws / "config").write_text("[default]\nregion = eu-west-1\n")
    r = run_authgate(tmp_path, "aws", "add", "second")
    assert r.returncode == 0, r.stderr

    r = run_authgate(tmp_path, "aws", "use", "first")
    assert r.returncode == 0, r.stderr
    assert "AKIA_first" in (aws / "credentials").read_text()
    assert "us-east-1" in (aws / "config").read_text()


def test_supabase_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    token_dir = tmp_path / ".supabase"
    token_dir.mkdir()
    token = token_dir / "access-token"
    token.write_text("sbp_first_token")
    monkeypatch.setenv("HOME", str(tmp_path))

    r = run_authgate(tmp_path, "supabase", "add", "first")
    assert r.returncode == 0, r.stderr

    token.write_text("sbp_second_token")
    r = run_authgate(tmp_path, "supabase", "add", "second")
    assert r.returncode == 0, r.stderr

    r = run_authgate(tmp_path, "supabase", "use", "first")
    assert r.returncode == 0, r.stderr
    assert token.read_text() == "sbp_first_token"


def test_list_empty(fake_home: Path):
    r = run_authgate(fake_home, "list")
    assert r.returncode == 0, r.stderr


def test_prompt_output(fake_home: Path):
    run_authgate(fake_home, "stripe", "add", "personal")
    r = run_authgate(fake_home, "prompt")
    assert r.returncode == 0, r.stderr
    assert "stripe:personal" in r.stdout


def test_prompt_filtered_to_service(fake_home: Path):
    run_authgate(fake_home, "stripe", "add", "personal")
    r = run_authgate(fake_home, "prompt", "stripe")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "stripe:personal"


def test_prompt_empty_when_no_profiles(fake_home: Path):
    r = run_authgate(fake_home, "prompt")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""


def test_add_and_use_roundtrip(fake_home: Path):
    cfg = fake_home / ".config" / "stripe" / "config.toml"
    original = cfg.read_text()

    r = run_authgate(fake_home, "stripe", "add", "first")
    assert r.returncode == 0, r.stderr

    state = json.loads((fake_home / ".authgate" / "state.json").read_text())
    assert state["stripe"] == "first"

    cfg.write_text("project-name = 'default'\n[default]\naccount_id = 'acct_BBB'\n")
    r = run_authgate(fake_home, "stripe", "add", "second")
    assert r.returncode == 0, r.stderr

    r = run_authgate(fake_home, "stripe", "use", "first")
    assert r.returncode == 0, r.stderr
    assert cfg.read_text() == original

    r = run_authgate(fake_home, "stripe", "use", "second")
    assert r.returncode == 0, r.stderr
    assert "BBB" in cfg.read_text()


def test_use_refuses_unsnapshotted_state(fake_home: Path):
    r = run_authgate(fake_home, "stripe", "use", "nonexistent")
    assert r.returncode != 0


def test_rm_clears_active(fake_home: Path):
    run_authgate(fake_home, "stripe", "add", "first")
    r = run_authgate(fake_home, "stripe", "rm", "first", "--force")
    assert r.returncode == 0, r.stderr
    state = json.loads((fake_home / ".authgate" / "state.json").read_text())
    assert "stripe" not in state


# --- rename ----------------------------------------------------------------


def test_rename_updates_active_marker(fake_home: Path):
    run_authgate(fake_home, "stripe", "add", "old")
    r = run_authgate(fake_home, "stripe", "rename", "old", "new")
    assert r.returncode == 0, r.stderr
    state = json.loads((fake_home / ".authgate" / "state.json").read_text())
    assert state["stripe"] == "new"
    assert not (fake_home / ".authgate" / "profiles" / "stripe" / "old").exists()
    assert (fake_home / ".authgate" / "profiles" / "stripe" / "new").exists()


def test_rename_refuses_collision_without_force(fake_home: Path):
    cfg = fake_home / ".config" / "stripe" / "config.toml"
    run_authgate(fake_home, "stripe", "add", "a")
    cfg.write_text("[default]\naccount_id = 'acct_other'\n")
    run_authgate(fake_home, "stripe", "add", "b")
    r = run_authgate(fake_home, "stripe", "rename", "a", "b")
    assert r.returncode != 0
    assert "already exists" in r.stderr or "already exists" in r.stdout


def test_rename_updates_groups(fake_home: Path):
    _seed_gh(fake_home, "userA")
    run_authgate(fake_home, "stripe", "add", "alpha")
    run_authgate(fake_home, "gh", "add", "alpha")
    r = run_authgate(fake_home, "group", "create", "team", "--stripe=alpha", "--gh=alpha")
    assert r.returncode == 0, r.stderr

    r = run_authgate(fake_home, "stripe", "rename", "alpha", "beta")
    assert r.returncode == 0, r.stderr
    assert "updated in groups" in r.stdout

    groups = json.loads((fake_home / ".authgate" / "groups.json").read_text())["groups"]
    assert groups["team"]["stripe"] == "beta"
    assert groups["team"]["gh"] == "alpha"


# --- groups: convention-based use -----------------------------------------


def test_top_level_use_convention(fake_home: Path):
    """`authgate use <name>` switches every service with a matching profile."""
    cfg_stripe = fake_home / ".config" / "stripe" / "config.toml"
    _seed_gh(fake_home, "userA")
    hosts_gh = fake_home / ".config" / "gh" / "hosts.yml"

    run_authgate(fake_home, "stripe", "add", "personal")
    run_authgate(fake_home, "gh", "add", "personal")

    # Mutate live state for both
    cfg_stripe.write_text("[default]\naccount_id = 'acct_OTHER'\n")
    hosts_gh.write_text("github.com:\n    user: other\n    oauth_token: other\n")
    run_authgate(fake_home, "stripe", "add", "work")
    run_authgate(fake_home, "gh", "add", "work")

    # Convention-based group switch
    r = run_authgate(fake_home, "use", "personal")
    assert r.returncode == 0, r.stderr
    assert "stripe   → personal" in r.stdout
    assert "gh       → personal" in r.stdout
    assert "acct_AAA" in cfg_stripe.read_text()
    assert "userA" in hosts_gh.read_text()


def test_top_level_use_unknown_name_fails(fake_home: Path):
    r = run_authgate(fake_home, "use", "nothing-matches-this")
    assert r.returncode != 0


# --- groups: explicit -----------------------------------------------------


def test_group_create_and_use(fake_home: Path):
    cfg_stripe = fake_home / ".config" / "stripe" / "config.toml"
    _seed_gh(fake_home, "userX")
    hosts_gh = fake_home / ".config" / "gh" / "hosts.yml"

    run_authgate(fake_home, "stripe", "add", "stripe-personal")
    run_authgate(fake_home, "gh", "add", "gh-personal")

    r = run_authgate(
        fake_home,
        "group",
        "create",
        "personal",
        "--stripe=stripe-personal",
        "--gh=gh-personal",
    )
    assert r.returncode == 0, r.stderr

    groups = json.loads((fake_home / ".authgate" / "groups.json").read_text())["groups"]
    assert groups["personal"] == {"stripe": "stripe-personal", "gh": "gh-personal"}

    # Mutate live state
    cfg_stripe.write_text("[default]\naccount_id = 'acct_DIFFERENT'\n")
    hosts_gh.write_text("github.com:\n    user: different\n    oauth_token: t\n")
    run_authgate(fake_home, "stripe", "add", "other")
    run_authgate(fake_home, "gh", "add", "other")

    # Use the explicit group
    r = run_authgate(fake_home, "use", "personal")
    assert r.returncode == 0, r.stderr
    assert "stripe   → stripe-personal" in r.stdout
    assert "gh       → gh-personal" in r.stdout
    assert "acct_AAA" in cfg_stripe.read_text()
    assert "userX" in hosts_gh.read_text()


def test_group_create_with_missing_profile_fails(fake_home: Path):
    run_authgate(fake_home, "stripe", "add", "real")
    r = run_authgate(fake_home, "group", "create", "bad", "--stripe=does-not-exist")
    assert r.returncode != 0
    assert not (fake_home / ".authgate" / "groups.json").exists()


def test_group_show_explicit(fake_home: Path):
    run_authgate(fake_home, "stripe", "add", "p")
    run_authgate(fake_home, "group", "create", "g1", "--stripe=p")
    r = run_authgate(fake_home, "group", "show", "g1")
    assert r.returncode == 0, r.stderr
    assert "explicit" in r.stdout
    assert "stripe" in r.stdout and "→ p" in r.stdout


def test_group_show_convention(fake_home: Path):
    run_authgate(fake_home, "stripe", "add", "shared-name")
    r = run_authgate(fake_home, "group", "show", "shared-name")
    assert r.returncode == 0, r.stderr
    assert "convention" in r.stdout


def test_group_rm(fake_home: Path):
    run_authgate(fake_home, "stripe", "add", "p")
    run_authgate(fake_home, "group", "create", "g1", "--stripe=p")
    r = run_authgate(fake_home, "group", "rm", "g1")
    assert r.returncode == 0, r.stderr
    groups = json.loads((fake_home / ".authgate" / "groups.json").read_text())["groups"]
    assert "g1" not in groups
