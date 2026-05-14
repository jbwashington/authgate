#!/usr/bin/env python3
"""authgate — switch user-level CLI auth between named profiles.

Profiles live under ~/.authgate/profiles/<service>/<name>/ and mirror the
on-disk auth state of each tool. Active profile per service is tracked in
~/.authgate/state.json.

Inspired by khanakia/vercelgate, extended to handle every user-level auth
CLI you've signed into.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

HOME = Path.home()
GATE_DIR = HOME / ".authgate"
PROFILES_DIR = GATE_DIR / "profiles"
STATE_FILE = GATE_DIR / "state.json"


@dataclass
class Service:
    key: str
    name: str
    paths: list[Path]
    whoami: list[str] | None = None
    excludes: list[str] = field(default_factory=list)

    def existing_paths(self) -> list[Path]:
        return [p for p in self.paths if p.exists()]


SERVICES: dict[str, Service] = {
    "cf": Service(
        key="cf",
        name="cloudflare",
        paths=[
            HOME / "Library/Preferences/.wrangler/config",
            HOME / ".cloudflared",
        ],
        whoami=["wrangler", "whoami"],
        excludes=["logs", "*.log", "*.bak"],
    ),
    "stripe": Service(
        key="stripe",
        name="stripe",
        paths=[HOME / ".config/stripe/config.toml"],
        whoami=["stripe", "config", "--list"],
    ),
    "vercel": Service(
        key="vercel",
        name="vercel",
        paths=[
            HOME / "Library/Application Support/com.vercel.cli/auth.json",
            HOME / "Library/Application Support/com.vercel.cli/config.json",
        ],
        whoami=["vercel", "whoami"],
    ),
    "gh": Service(
        key="gh",
        name="github",
        paths=[
            HOME / ".config/gh/hosts.yml",
            HOME / ".config/gh/config.yml",
        ],
        whoami=["gh", "auth", "status"],
    ),
    "doctl": Service(
        key="doctl",
        name="digitalocean",
        paths=[HOME / "Library/Application Support/doctl/config.yaml"],
        whoami=["doctl", "account", "get"],
    ),
}


# --- state -----------------------------------------------------------------


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    return json.loads(STATE_FILE.read_text())


def save_state(state: dict) -> None:
    GATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.chmod(STATE_FILE, 0o600)


def active_profile(svc_key: str) -> str | None:
    return load_state().get(svc_key)


def set_active(svc_key: str, name: str | None) -> None:
    state = load_state()
    if name is None:
        state.pop(svc_key, None)
    else:
        state[svc_key] = name
    save_state(state)


# --- snapshot / restore ----------------------------------------------------


def profile_dir(svc: Service, name: str) -> Path:
    return PROFILES_DIR / svc.key / name


def list_profiles(svc: Service) -> list[str]:
    d = PROFILES_DIR / svc.key
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir())


def _ignore_factory(excludes: list[str]) -> Callable[[str, list[str]], Iterable[str]]:
    if not excludes:
        return lambda _d, _f: []
    return shutil.ignore_patterns(*excludes)


def snapshot(svc: Service, dest: Path) -> int:
    """Copy current live auth state into dest/. Returns number of items copied."""
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    n = 0
    for src in svc.paths:
        if not src.exists():
            continue
        target = dest / src.name
        if src.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(src, target, ignore=_ignore_factory(svc.excludes), symlinks=False)
        else:
            shutil.copy2(src, target)
        n += 1
    return n


def restore(svc: Service, source: Path) -> int:
    """Copy profile from source/ over the live auth state. Returns number of items restored."""
    n = 0
    for live in svc.paths:
        candidate = source / live.name
        if not candidate.exists():
            # profile doesn't have this path — leave whatever is there alone or remove?
            # Safest: if live exists but profile doesn't, do NOT touch (avoid surprise deletes).
            continue
        live.parent.mkdir(parents=True, exist_ok=True)
        if live.exists():
            if live.is_dir():
                shutil.rmtree(live)
            else:
                live.unlink()
        if candidate.is_dir():
            shutil.copytree(candidate, live, symlinks=False)
        else:
            shutil.copy2(candidate, live)
        n += 1
    return n


# --- commands --------------------------------------------------------------


def resolve_service(key: str) -> Service:
    if key not in SERVICES:
        die(f"unknown service '{key}'. Known: {', '.join(SERVICES)}")
    return SERVICES[key]


def cmd_services(_args) -> None:
    for svc in SERVICES.values():
        installed = any(p.exists() for p in svc.paths)
        marker = "✓" if installed else " "
        print(f"  {marker} {svc.key:8} ({svc.name})")


def cmd_list_all(_args) -> None:
    state = load_state()
    any_profile = False
    for svc in SERVICES.values():
        profiles = list_profiles(svc)
        live_present = any(p.exists() for p in svc.paths)
        if not profiles and not live_present:
            continue
        any_profile = True
        active = state.get(svc.key)
        rendered = []
        for p in profiles:
            rendered.append(f"{p}*" if p == active else p)
        if not rendered:
            rendered.append("(none — live state not yet snapshotted)")
        print(f"  {svc.key:8} {', '.join(rendered)}")
    if not any_profile:
        print("  (no services configured yet — run `authgate <svc> add <name>` to start)")


def cmd_list(args) -> None:
    if not args.service:
        cmd_list_all(args)
        return
    svc = resolve_service(args.service)
    profiles = list_profiles(svc)
    active = active_profile(svc.key)
    if not profiles:
        print(f"no profiles for {svc.name}. Use `authgate {svc.key} add <name>` to capture current state.")
        return
    for p in profiles:
        marker = " * " if p == active else "   "
        print(f"{marker}{p}")


def cmd_current(args) -> None:
    svc = resolve_service(args.service)
    active = active_profile(svc.key)
    live_present = any(p.exists() for p in svc.paths)
    if active:
        print(f"active: {active}")
    elif live_present:
        print("active: <unsnapshotted live state>")
    else:
        print("active: <none — not logged in>")
    if svc.whoami and live_present:
        print(f"\n$ {' '.join(svc.whoami)}")
        try:
            r = subprocess.run(svc.whoami, capture_output=True, text=True, timeout=15)
            sys.stdout.write(r.stdout)
            if r.returncode != 0 and r.stderr:
                sys.stderr.write(r.stderr)
        except FileNotFoundError:
            print(f"  (cli '{svc.whoami[0]}' not found in PATH)", file=sys.stderr)
        except subprocess.TimeoutExpired:
            print("  (whoami timed out)", file=sys.stderr)


def cmd_add(args) -> None:
    svc = resolve_service(args.service)
    name = args.name
    if not any(p.exists() for p in svc.paths):
        die(f"no live auth state for {svc.name} — log in first with the {svc.key} CLI.")
    target = profile_dir(svc, name)
    if target.exists() and not args.force:
        die(f"profile '{name}' already exists for {svc.name}. Use --force to overwrite.")
    if target.exists():
        shutil.rmtree(target)
    n = snapshot(svc, target)
    set_active(svc.key, name)
    print(f"captured {n} path(s) → profile '{name}' (now active for {svc.name})")


def cmd_use(args) -> None:
    svc = resolve_service(args.service)
    name = args.name
    target = profile_dir(svc, name)
    if not target.exists():
        existing = list_profiles(svc)
        hint = f" Known: {', '.join(existing)}" if existing else " (none captured yet)"
        die(f"no profile '{name}' for {svc.name}.{hint}")
    # Safety: sync current live state back to whichever profile is currently marked active,
    # so token rotations (refresh_token churn) don't get lost.
    current = active_profile(svc.key)
    live_present = any(p.exists() for p in svc.paths)
    if live_present:
        if current is None:
            if not args.force:
                die(
                    f"live {svc.name} auth exists but no profile is marked active.\n"
                    f"Run `authgate {svc.key} add <name>` to capture it first, or pass --force to discard."
                )
        elif current != name:
            snapshot(svc, profile_dir(svc, current))
    n = restore(svc, target)
    set_active(svc.key, name)
    print(f"restored {n} path(s) ← profile '{name}' ({svc.name})")
    if svc.whoami:
        try:
            r = subprocess.run(svc.whoami, capture_output=True, text=True, timeout=15)
            first_line = (r.stdout or r.stderr).strip().splitlines()[:1]
            if first_line:
                print(f"  {first_line[0]}")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass


def cmd_rm(args) -> None:
    svc = resolve_service(args.service)
    name = args.name
    target = profile_dir(svc, name)
    if not target.exists():
        die(f"no profile '{name}' for {svc.name}.")
    if active_profile(svc.key) == name and not args.force:
        die(f"'{name}' is the active {svc.name} profile. Pass --force to remove anyway.")
    shutil.rmtree(target)
    if active_profile(svc.key) == name:
        set_active(svc.key, None)
    print(f"removed profile '{name}' from {svc.name}")


# --- cli wiring ------------------------------------------------------------


def die(msg: str, code: int = 1) -> "None":
    print(f"authgate: {msg}", file=sys.stderr)
    sys.exit(code)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="authgate",
        description="Switch user-level CLI auth between named profiles.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # top-level commands
    ls = sub.add_parser("list", help="list services and their profiles")
    ls.add_argument("service", nargs="?", help="optional service key (cf, stripe, ...)")
    ls.set_defaults(func=cmd_list)

    sub.add_parser("services", help="show supported services").set_defaults(func=cmd_services)

    # per-service commands take form: authgate <svc> <verb> [name]
    # We also expose: authgate <verb> <svc> [name] won't be supported — keep it tight.
    for svc_key in SERVICES:
        sp = sub.add_parser(svc_key, help=f"manage {SERVICES[svc_key].name} profiles")
        svc_sub = sp.add_subparsers(dest="verb", required=True)

        cur = svc_sub.add_parser("current", help="show active profile + whoami")
        cur.set_defaults(func=cmd_current, service=svc_key)

        lst = svc_sub.add_parser("list", help="list profiles for this service")
        lst.set_defaults(func=cmd_list, service=svc_key)

        add = svc_sub.add_parser("add", help="capture current live auth as a profile")
        add.add_argument("name")
        add.add_argument("--force", action="store_true", help="overwrite if profile exists")
        add.set_defaults(func=cmd_add, service=svc_key)

        use = svc_sub.add_parser("use", help="switch to a named profile")
        use.add_argument("name")
        use.add_argument("--force", action="store_true", help="discard unsnapshotted live state")
        use.set_defaults(func=cmd_use, service=svc_key)

        rm = svc_sub.add_parser("rm", help="delete a profile")
        rm.add_argument("name")
        rm.add_argument("--force", action="store_true", help="remove even if active")
        rm.set_defaults(func=cmd_rm, service=svc_key)

    return p


def main(argv: list[str] | None = None) -> None:
    GATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(GATE_DIR, 0o700)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
