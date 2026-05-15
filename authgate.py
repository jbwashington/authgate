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
GROUPS_FILE = GATE_DIR / "groups.json"


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
    "supabase": Service(
        key="supabase",
        name="supabase",
        paths=[HOME / ".supabase/access-token"],
        # supabase cli has no quick `whoami`; skip the identity print on switch
    ),
    "claude": Service(
        key="claude",
        name="claude-code",
        # Only the credential file — NOT the rest of ~/.claude/, which is user
        # settings, agents, projects, MCP config, etc. that shouldn't change
        # per Anthropic account.
        paths=[HOME / ".claude/.credentials.json"],
    ),
    "codex": Service(
        key="codex",
        name="codex",
        paths=[HOME / ".codex/auth.json"],
    ),
    "aws": Service(
        key="aws",
        name="aws",
        paths=[
            HOME / ".aws/config",
            HOME / ".aws/credentials",
        ],
        whoami=["aws", "sts", "get-caller-identity"],
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


# --- groups ----------------------------------------------------------------


def load_groups() -> dict:
    if not GROUPS_FILE.exists():
        return {}
    return json.loads(GROUPS_FILE.read_text()).get("groups", {})


def save_groups(groups: dict) -> None:
    GATE_DIR.mkdir(parents=True, exist_ok=True)
    GROUPS_FILE.write_text(json.dumps({"groups": groups}, indent=2, sort_keys=True))
    os.chmod(GROUPS_FILE, 0o600)


def resolve_group(name: str) -> dict[str, str]:
    """Return svc_key → profile_name mapping for `name`.

    Explicit groups (~/.authgate/groups.json) take precedence. If no explicit
    group exists, fall back to convention: every service that has a profile
    named `name` is included with that profile.
    """
    groups = load_groups()
    if name in groups:
        return dict(groups[name])
    convention = {k: name for k, svc in SERVICES.items() if profile_dir(svc, name).exists()}
    if not convention:
        die(
            f"no group or profile named '{name}'. "
            f"Run `authgate list` to see what's available."
        )
    return convention


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
    groups = load_groups()
    if groups:
        print("\ngroups:")
        for name in sorted(groups):
            pairs = ", ".join(f"{k}={v}" for k, v in sorted(groups[name].items()))
            print(f"  {name:10} {pairs}")


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


def cmd_prompt(args) -> None:
    """Compact active-profile indicator for tmux status bars / shell prompts.

    Reads only ~/.authgate/state.json — no subprocesses — so it is cheap
    enough to call on every status refresh or prompt render.
    """
    state = load_state()
    keys = args.services or list(SERVICES)
    parts = []
    for svc_key in keys:
        if svc_key not in SERVICES:
            continue
        active = state.get(svc_key)
        if active:
            parts.append(f"{svc_key}:{active}")
        elif list_profiles(SERVICES[svc_key]):
            # profiles exist but none marked active
            parts.append(f"{svc_key}:?")
    if parts:
        print(args.separator.join(parts))


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


def _switch_service(svc: Service, name: str, *, force: bool = False) -> int:
    """Snapshot current live state back to the previously-active profile,
    then restore the named profile. Returns number of paths restored.
    """
    target = profile_dir(svc, name)
    if not target.exists():
        existing = list_profiles(svc)
        hint = f" Known: {', '.join(existing)}" if existing else " (none captured yet)"
        die(f"no profile '{name}' for {svc.name}.{hint}")
    current = active_profile(svc.key)
    live_present = any(p.exists() for p in svc.paths)
    if live_present:
        if current is None:
            if not force:
                die(
                    f"live {svc.name} auth exists but no profile is marked active.\n"
                    f"Run `authgate {svc.key} add <name>` to capture it first, or pass --force to discard."
                )
        elif current != name:
            snapshot(svc, profile_dir(svc, current))
    n = restore(svc, target)
    set_active(svc.key, name)
    return n


def cmd_use(args) -> None:
    svc = resolve_service(args.service)
    n = _switch_service(svc, args.name, force=args.force)
    print(f"restored {n} path(s) ← profile '{args.name}' ({svc.name})")
    if svc.whoami:
        try:
            r = subprocess.run(svc.whoami, capture_output=True, text=True, timeout=15)
            first_line = (r.stdout or r.stderr).strip().splitlines()[:1]
            if first_line:
                print(f"  {first_line[0]}")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass


def cmd_top_use(args) -> None:
    """`authgate use <name>` — switch every service the group/convention covers."""
    name = args.name
    mapping = resolve_group(name)
    missing = [
        f"{svc_key}:{profile}"
        for svc_key, profile in mapping.items()
        if not profile_dir(SERVICES[svc_key], profile).exists()
    ]
    if missing:
        die(f"group '{name}' references missing profiles: {', '.join(missing)}")
    is_explicit = name in load_groups()
    print(f"switching to {'group' if is_explicit else 'profile'} '{name}'")
    for svc_key, profile in mapping.items():
        svc = SERVICES[svc_key]
        _switch_service(svc, profile, force=args.force)
        print(f"  {svc_key:8} → {profile}")
    skipped = [k for k in SERVICES if k not in mapping]
    if skipped:
        print(f"  (skipped: {', '.join(skipped)})")


def cmd_rename(args) -> None:
    svc = resolve_service(args.service)
    src = profile_dir(svc, args.old)
    dst = profile_dir(svc, args.new)
    if not src.exists():
        die(f"no profile '{args.old}' for {svc.name}.")
    if args.old == args.new:
        die("old and new names are the same.")
    if dst.exists() and not args.force:
        die(f"profile '{args.new}' already exists for {svc.name}. Use --force to overwrite.")
    if dst.exists():
        shutil.rmtree(dst)
    src.rename(dst)
    if active_profile(svc.key) == args.old:
        set_active(svc.key, args.new)
    groups = load_groups()
    touched_groups = []
    for gname, mapping in groups.items():
        if mapping.get(svc.key) == args.old:
            mapping[svc.key] = args.new
            touched_groups.append(gname)
    if touched_groups:
        save_groups(groups)
    msg = f"renamed {svc.name} profile '{args.old}' → '{args.new}'"
    if touched_groups:
        msg += f" (updated in groups: {', '.join(touched_groups)})"
    print(msg)


def cmd_group_list(args) -> None:
    groups = load_groups()
    if not groups:
        print(
            "no explicit groups defined.\n"
            "  `authgate use <name>` switches every service that has a profile named <name>.\n"
            "  Define an explicit mapping with `authgate group create <name> --cf=foo --vercel=bar`."
        )
        return
    for name in sorted(groups):
        pairs = ", ".join(f"{k}={v}" for k, v in sorted(groups[name].items()))
        print(f"  {name}: {pairs}")


def cmd_group_show(args) -> None:
    name = args.name
    groups = load_groups()
    if name in groups:
        print(f"group '{name}' (explicit):")
        for k, v in sorted(groups[name].items()):
            print(f"  {k:8} → {v}")
        return
    convention = {k: name for k, svc in SERVICES.items() if profile_dir(svc, name).exists()}
    if convention:
        print(f"'{name}' (convention — no explicit group; would switch:)")
        for k, v in sorted(convention.items()):
            print(f"  {k:8} → {v}")
    else:
        die(f"no group or profile named '{name}'.")


def cmd_group_create(args) -> None:
    name = args.name
    mapping: dict[str, str] = {}
    for svc_key in SERVICES:
        value = getattr(args, svc_key, None)
        if not value:
            continue
        svc = SERVICES[svc_key]
        if not profile_dir(svc, value).exists():
            die(
                f"profile '{value}' does not exist for {svc.name}. "
                f"Existing: {', '.join(list_profiles(svc)) or '(none)'}"
            )
        mapping[svc_key] = value
    if not mapping:
        die(f"group '{name}' would be empty. Pass at least one --<svc>=<profile>.")
    groups = load_groups()
    existed = name in groups
    groups[name] = mapping
    save_groups(groups)
    pairs = ", ".join(f"{k}={v}" for k, v in sorted(mapping.items()))
    print(f"{'updated' if existed else 'created'} group '{name}': {pairs}")


def cmd_group_rm(args) -> None:
    groups = load_groups()
    if args.name not in groups:
        die(f"no group '{args.name}'.")
    del groups[args.name]
    save_groups(groups)
    print(f"removed group '{args.name}'.")


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

    pr = sub.add_parser(
        "prompt",
        help="compact active-profile indicator for tmux / shell prompts",
    )
    pr.add_argument("services", nargs="*", help="limit output to specific service keys")
    pr.add_argument(
        "--separator",
        default=" ",
        help="string between entries (default: a space)",
    )
    pr.set_defaults(func=cmd_prompt)

    top_use = sub.add_parser(
        "use",
        help="switch every service to a group or shared profile name",
    )
    top_use.add_argument("name", help="explicit group name or a profile name shared across services")
    top_use.add_argument("--force", action="store_true", help="discard unsnapshotted live state")
    top_use.set_defaults(func=cmd_top_use)

    # `authgate group` subtree
    group = sub.add_parser("group", help="manage explicit cross-service groups")
    group_sub = group.add_subparsers(dest="verb", required=True)

    g_list = group_sub.add_parser("list", help="list defined groups")
    g_list.set_defaults(func=cmd_group_list)

    g_show = group_sub.add_parser("show", help="show what a group/profile-name would switch")
    g_show.add_argument("name")
    g_show.set_defaults(func=cmd_group_show)

    g_create = group_sub.add_parser("create", help="create or update an explicit group")
    g_create.add_argument("name")
    for svc_key in SERVICES:
        g_create.add_argument(
            f"--{svc_key}",
            metavar="PROFILE",
            help=f"profile name for {SERVICES[svc_key].name}",
        )
    g_create.set_defaults(func=cmd_group_create)

    g_rm = group_sub.add_parser("rm", help="delete a group")
    g_rm.add_argument("name")
    g_rm.set_defaults(func=cmd_group_rm)

    # per-service commands: authgate <svc> <verb> [args]
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

        ren = svc_sub.add_parser("rename", help="rename a profile (updates groups + active marker)")
        ren.add_argument("old")
        ren.add_argument("new")
        ren.add_argument("--force", action="store_true", help="overwrite if target name exists")
        ren.set_defaults(func=cmd_rename, service=svc_key)

    return p


def main(argv: list[str] | None = None) -> None:
    GATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(GATE_DIR, 0o700)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
