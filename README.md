# authgate

> Switch user-level CLI auth between named profiles. One command instead of `logout && login` for every account swap.

Most developer CLIs (`wrangler`, `stripe`, `vercel`, `gh`, `doctl`) only support one logged-in session at a time. If you work across multiple accounts — a personal account and a client/employer account, say — switching means a full logout/login dance, every time, for every tool.

`authgate` snapshots each tool's on-disk auth state into named profiles and swaps them on demand. Inspired by [vercelgate](https://github.com/khanakia/vercelgate), generalized to every user-level auth CLI on your machine.

```
$ authgate list
  cf       personal*, work
  stripe   personal*, work
  vercel   personal, work*
  gh       personal*

$ authgate use work                    # switch every service to its 'work' profile
switching to profile 'work'
  cf       → work
  stripe   → work
  vercel   → work
  (skipped: gh, doctl)

$ authgate cf use personal             # or switch one service at a time
restored 2 path(s) ← profile 'personal' (cloudflare)
👋 You are logged in with an OAuth Token, associated with the email me@personal.com.
```

## Supported services

| Key        | Tool                  | What gets swapped                                                |
| ---------- | --------------------- | ---------------------------------------------------------------- |
| `cf`       | Cloudflare wrangler + cloudflared | `~/Library/Preferences/.wrangler/config/` + `~/.cloudflared/` |
| `stripe`   | Stripe CLI            | `~/.config/stripe/config.toml`                                   |
| `vercel`   | Vercel CLI            | `~/Library/Application Support/com.vercel.cli/{auth,config}.json` |
| `gh`       | GitHub CLI            | `~/.config/gh/{hosts,config}.yml`                                |
| `doctl`    | DigitalOcean doctl    | `~/Library/Application Support/doctl/config.yaml`                |
| `supabase` | Supabase CLI          | `~/.supabase/access-token`                                       |
| `claude`   | Claude Code           | `~/.claude/.credentials.json` (only the credentials file — your settings, agents, MCP config, and projects are not swapped) |
| `codex`    | OpenAI Codex          | `~/.codex/auth.json`                                             |
| `aws`      | AWS CLI               | `~/.aws/config` + `~/.aws/credentials`                           |

Paths shown are macOS — Linux variants are handled automatically where the CLI follows XDG conventions.

## Install

### Homebrew (macOS/Linux)

```sh
brew install jbwashington/tap/authgate
```

Ships a zsh completion to `$(brew --prefix)/share/zsh/site-functions/_authgate`. Open a new shell, then `authgate <TAB>` cycles through services and verbs; `authgate cf use <TAB>` completes the profile names you've saved.

### pipx (any platform)

```sh
pipx install git+https://github.com/jbwashington/authgate
```

### From source

```sh
git clone https://github.com/jbwashington/authgate
cd authgate
pipx install -e .
```

`authgate` is a single-file Python 3.9+ module with zero runtime dependencies.

## Usage

### Capture the account you're currently logged into

```sh
authgate cf add personal     # snapshots current wrangler/cloudflared state
authgate stripe add personal
authgate vercel add personal
authgate gh add personal
```

### Add a second account

Log into the other account with the native CLI, then snapshot:

```sh
wrangler logout && wrangler login   # browser flow for the other account
authgate cf add work
```

### Switch

```sh
authgate cf use work                   # one service
authgate cf use personal

authgate use work                      # all services at once (see Groups below)
```

### Inspect

```sh
authgate list              # all services + profiles + groups, active marked with *
authgate cf list           # profiles for one service
authgate cf current        # active profile + `wrangler whoami` output
authgate services          # supported tools, marked ✓ if you have them installed
authgate group show work   # what `authgate use work` would do
```

### Rename a profile

```sh
authgate cf rename foo bar
```

Renames the profile dir, updates the active-marker if needed, and rewrites any explicit groups that referenced it.

### Remove a profile

```sh
authgate cf rm work
```

## Groups

Switching all your services at once usually means flipping a coordinated set of accounts together: "I'm working on the Titan project today" → flip Cloudflare, Vercel, and Stripe simultaneously. authgate handles this two ways:

### Convention-based (no setup needed)

If you name your profiles consistently across services — `cf:titan`, `vercel:titan`, `stripe:titan` — then a single `authgate use titan` switches all of them. Services that don't have a profile by that name are silently skipped. No config file, no `group create`.

```sh
authgate use titan
# switching to profile 'titan'
#   cf       → titan
#   stripe   → titan
#   vercel   → titan
#   (skipped: gh, doctl)
```

### Explicit groups (when names don't line up)

If your profile names diverge — say Vercel is `7itantech` but Cloudflare is just `titan` — define an explicit mapping. Stored in `~/.authgate/groups.json`.

```sh
authgate group create titan-mixed \
  --cf=titan \
  --vercel=7itantech \
  --stripe=fullstack

authgate use titan-mixed
# switching to group 'titan-mixed'
#   cf       → titan
#   stripe   → fullstack
#   vercel   → 7itantech
```

Explicit groups take precedence over the convention when both apply.

```sh
authgate group list                    # show all groups
authgate group show titan-mixed        # preview what a name will do (group or convention)
authgate group rm titan-mixed
```

When you rename a profile, any explicit group that referenced it is auto-updated.

## Status bar / prompt indicator

`authgate prompt` emits a compact one-liner of the active profile per service — meant for a tmux status bar or shell prompt so you always see which accounts are live.

By default each service is shown as its **brand icon** (a [Nerd Font](https://www.nerdfonts.com/) glyph) instead of its text key, so the line stays short:

```sh
$ authgate prompt                  #  titan   personal   personal   titan
                                   # (cf, gh, stripe, vercel logos)

$ authgate prompt cf vercel        # limit to specific services

$ authgate prompt --labels         # text keys instead of icons:
cf:titan gh:personal stripe:personal vercel:titan
```

The icons require a Nerd Font in your terminal. If you don't have one, pass `--labels` (or set `AUTHGATE_PROMPT_LABELS=1`) to fall back to `cf:`-style text keys. Services with no defined glyph fall back to their text key automatically.

It reads only `~/.authgate/state.json` — no subprocesses — so it's cheap to call on every refresh.

### tmux

Add it to `status-left` in `~/.tmux.conf`:

```tmux
set -g status-left-length 200
set -g status-left "#[fg=cyan] #(authgate prompt) #[default]#S "
```

tmux re-runs it every `status-interval` seconds (default 15).

**Put it in `status-left`, not `status-right`.** tmux truncates `status-right` from the *left* when the bar is wider than the terminal — so an indicator placed there is the first thing clipped. `status-left` truncates from the right, so a segment at its start survives. If you only track one or two services, `authgate prompt cf` keeps it short enough for either side.

### zsh

Right-side prompt, refreshed before each prompt draw:

```zsh
precmd() { RPROMPT="%F{cyan}$(authgate prompt)%f" }
```

## How it works

Profiles live at `~/.authgate/profiles/<service>/<name>/` as exact mirrors of the source auth files. The active profile per service is tracked in `~/.authgate/state.json`.

**Token-rotation safety:** when you `use` a different profile, the current live state is first snapshotted back into the previously-active profile. So if `wrangler` rotated its refresh token while `personal` was active, that fresh token gets written back to the `personal` profile before `work` is restored. No token loss across switches.

**Refusal-on-ambiguity:** if there's live auth state but no profile is marked active, `use` refuses to overwrite (pass `--force` to discard). This prevents accidentally wiping a session you forgot to snapshot.

## Security notes

- The `~/.authgate/` directory is created with mode `0700` and the state file with `0600`.
- Profile contents are exact copies of your CLI auth files (OAuth tokens, refresh tokens, API keys). Treat the profile directory like you'd treat `~/.ssh/` — don't sync it through services that flatten permissions, don't commit it, don't share it.
- Each machine maintains its own profiles. Do not copy `~/.authgate/profiles/` between hosts. Tokens are often device-bound, and refresh rotation will diverge.

## Adding a new service

Open a PR. Each service is ~10 lines in the `SERVICES` registry:

```python
"myservice": Service(
    key="myservice",
    name="my-service",
    paths=[HOME / ".config/myservice/credentials"],
    whoami=["myservice", "whoami"],
),
```

Things to verify before submitting:

1. The file(s) actually contain the auth state (not just a cached display preference).
2. The CLI tolerates the file changing between invocations (it will, if it reads on every run).
3. `whoami` is a fast, non-interactive command that confirms the identity.

## Limitations

- **Single-machine.** Profiles aren't designed to be portable between hosts.
- **No tunnel-aware cloudflared awareness.** If a `cloudflared` tunnel is running when you swap CF accounts, it'll keep using its in-memory credentials until restart, then fail to find its credentials file. Stop tunnels before swapping.
- **`vercel switch`** exists for teams within one account; this is for switching between separate accounts.
- **aws already has native multi-account.** `aws --profile <name>` covers most cases without authgate. Use `authgate aws use <name>` when you specifically want the *unqualified* `aws` command to flip wholesale (rather than passing a flag everywhere).
- **`gcloud` is not included** — it already has first-class multi-account support via `gcloud config configurations`. Use that.

## License

MIT — see [LICENSE](LICENSE).
