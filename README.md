# dotfiles

Managed with [chezmoi](https://chezmoi.io). Supports:
- **Online machines** — macOS (work laptop), personal Linux
- **Offline machines** — Yandex VMs (no internet, `*.yp-c.yandex.net`)

## New online machine

```sh
# 1. Prerequisites
sudo apt install zsh git curl   # Debian/Ubuntu
# brew install zsh git           # macOS (git/zsh usually pre-installed)

# 2. Install chezmoi
sh -c "$(curl -fsLS get.chezmoi.io)"

# 3. Init + apply (will prompt: work machine? has GUI?)
# Also installs Homebrew + packages from .chezmoidata/packages.toml (see Packages below)
chezmoi init --apply git@github.com:romash1408/dotfiles.git

# 4. Set zsh as default shell
zsh_path=$(which zsh)
grep -qxF "$zsh_path" /etc/shells || sudo sh -c "echo $zsh_path >> /etc/shells"
chsh -s "$zsh_path"
```

Re-login or open a new terminal. Done.

## New server over SSH (headless, non-TTY)

The repo is public, so `https` init works without GitHub keys. `promptBoolOnce`
can't ask questions without a TTY — pre-seed them with `--promptBool`:

```sh
host=kg   # ssh alias

# 1. Age key (from a machine that already has it)
ssh $host 'mkdir -p ~/.config/chezmoi'
scp ~/.config/chezmoi/key.txt $host:.config/chezmoi/key.txt
ssh $host 'chmod 600 ~/.config/chezmoi/key.txt'

# 2. chezmoi binary
ssh $host 'sh -c "$(curl -fsLS get.chezmoi.io)" -- -b ~/.local/bin'

# 3. Init + apply (--force: without TTY chezmoi can't ask about overwriting
#    a manually-installed ~/.oh-my-zsh etc.)
ssh $host '~/.local/bin/chezmoi init --apply --force --promptBool work=false,hasGUI=false https://github.com/romash1408/dotfiles.git'
```

Homebrew install needs `sudo` for `/home/linuxbrew` — the user must have
passwordless sudo (true on kg/cloud/backup), otherwise run apply from an
interactive ssh session instead.

Seen in practice (2026-07, all three servers):
- `chezmoi init` cloned a stale commit — check `git -C ~/.local/share/chezmoi log -1`
  after init and run `chezmoi update --force` if it's behind `origin/main`.
- If `chezmoi update` fails with "git: exit status 1" about upstream:
  `git -C ~/.local/share/chezmoi branch --set-upstream-to=origin/main main`

## Termux (Android)

No Homebrew there (it needs glibc, Android has bionic), so packages come from
`pkg`. chezmoi from `pkg` is built with `GOOS=android`, so `.chezmoi.os` is
`"android"` — every `eq .chezmoi.os "linux"` branch (apt, linuxbrew, herdr
systemd unit) is skipped automatically.

```sh
# 1. Prerequisites (chezmoi itself from pkg, not get.chezmoi.io)
pkg install -y chezmoi git zsh openssh

# 2. Age key (see below)
mkdir -p ~/.config/chezmoi && chmod 700 ~/.config/chezmoi
# ...put key.txt there, chmod 600

# 3. Init + apply — installs packages.android.pkg via `pkg install`
chezmoi init --apply https://github.com/romash1408/dotfiles.git

# 4. zsh as default shell (Termux's own chsh, no /etc/shells)
chsh -s zsh
```

Scripts run by chezmoi must not hardcode `#!/bin/bash`: there is no `/bin` on
Android, and chezmoi (a Go binary) bypasses termux-exec, which rewrites such
shebangs for ordinary shells. Use `{{ template "bash-shebang" . }}` as the first
line of bash `run_` scripts that can render on Termux — it resolves bash via
`lookPath` there and stays `#!/bin/bash` everywhere else.

## Age encryption key

Secrets (tokens, SSH keys) are stored in the repo encrypted with [age](https://age-encryption.org).
chezmoi decrypts them automatically at apply time using `~/.config/chezmoi/key.txt`.

**The key is NOT in the repo** — you must place it on each machine manually before running `chezmoi init`.

### Getting the key

The key is backed up in Bitwarden as **"chezmoi-age-key"**:

```sh
rbw get "chezmoi-age-key"
```

### Placing the key

```sh
mkdir -p ~/.config/chezmoi
# paste the AGE-SECRET-KEY-1... line from Bitwarden:
rbw get "chezmoi-age-key" > ~/.config/chezmoi/key.txt
chmod 600 ~/.config/chezmoi/key.txt
```

On machines without `rbw`, copy the key manually (e.g. over SSH or from a password manager):

```sh
# from another machine:
scp ~/.config/chezmoi/key.txt new-host:.config/chezmoi/key.txt
ssh new-host "chmod 600 ~/.config/chezmoi/key.txt"
```

`./chezmoi-upload-offline.sh` copies the key to offline machines automatically.

> **`offline` is auto-detected** from the hostname suffix `.yp-c.yandex.net` —
> no need to pass it manually. On offline machines `chezmoi init` will fail
> (no internet); use the offline flow below instead.

## New offline machine (Yandex VM, no internet)

Run this **from your macOS work laptop** — it handles everything over SSH:

```sh
# First time — bootstraps chezmoi + clones all repos on the remote:
./chezmoi-upload-offline.sh <ssh-alias> work=true hasGUI=false

# Then on the remote machine, set zsh as default:
ssh <ssh-alias> "sudo apt install zsh -y && chsh -s \$(which zsh)"
```

`KEY=VALUE` args pre-seed the interactive `promptBoolOnce` questions that
chezmoi can't ask in a non-TTY context. `offline` is skipped — it is
auto-detected from the hostname.

### How it works

1. Uploads the `chezmoi` binary if missing (`~/.local/bin/chezmoi`)
2. For each repo (`dotfiles`, `oh-my-zsh`, `zsh-autosuggestions`):
   - Initialises a bare repo on the remote (`~/repo-name.git`)
   - Adds a local git remote named `<ssh-alias>`
   - Pushes
3. Runs `chezmoi init ~/dotfiles.git` + `chezmoi apply` on the remote
4. `run_after_pull` (triggered by apply) clones `oh-my-zsh` and plugins
   from the bare repos into their working directories (`chmod 700`)

## Packages

`run_onchange_before_install-packages.sh` installs [Homebrew](https://brew.sh)
(macOS `/opt/homebrew`, Linux `/home/linuxbrew/.linuxbrew`) and everything from
`.chezmoidata/packages.toml` via a single `brew bundle`:

| List | Installed on |
|------|--------------|
| `packages.brews` | everywhere (CLI tools) |
| `packages.darwin.casks` | macOS (GUI apps) |
| `packages.linux.flatpaks` | Linux with `hasGUI=true` (flatpak + flathub are set up automatically) |
| `packages.linux.apt` | Linux, via `apt` (tmux, net-tools — must match system daemons) |
| `packages.android.pkg` | Termux, via `pkg` — **instead of** everything above, no brew there |

- Skipped entirely on offline machines.
- Re-runs automatically on `chezmoi apply` whenever the package list changes.
- On servers: system packages (services, monitoring) live in ansible (`~/infra`);
  this list is only for tools I run by hand (htop, mc, kubectl, ...) — brew keeps
  them user-space and out of ansible's way.
- Homebrew on Linux needs x86_64 or ARM64 with glibc (Tier 1 since brew 5.0).
  Won't work in Termux (bionic libc) — there the script uses `pkg` and the
  `packages.android.pkg` list instead (see Termux above). Keep it in sync with
  `packages.brews` when adding tools.
- On Linux with glibc < 2.35 (Debian 11 and older) the script skips packages
  entirely: there are no usable bottles, brew builds the whole toolchain from
  source — that took down backup (1GB RAM) for an hour. Upgrade the OS first.

To add a package: edit `.chezmoidata/packages.toml`, run `chezmoi apply`.

## Day-to-day

### Explicit tool delivery to offline Linux machines

The existing apt/Homebrew/Termux package installer is unchanged: offline hosts
skip it, Termux uses `pkg`, and online Linux retains its glibc/Brew checks.
`tools-sync` adds a separate, explicit user-space delivery path. It does not run
from `chezmoi apply`, install system packages, or restart herdr/agents.

```sh
tools-sync dev --check --group agents --group common
tools-sync dev --group common --apply
tools-sync dev --group agents --apply --adopt
tools-sync new-vm --tool herdr --apply
tools-sync dev --tool codex --rollback
```

Versions and supported artifact recipes live in `.config/tools-sync/catalog.json`.
Edit that file in chezmoi and explicitly run the command to update. The initial
`common` group contains ripgrep; `agents` contains herdr, Codex CLI and Claude Code.
This is an allowlisted artifact installer, not a portable replacement for apt.
Python 3 and SSH are required on the target; the npm agents also need Node/npm.
Linux x64/arm64 are supported; Termux and macOS keep their existing package managers.

For npm agents the target's configured registry is tried first in a private
prefix. If unavailable, the online machine prepares a Linux-specific npm bundle
with install scripts disabled and transfers it by SSH. npm validates package
integrity; GitHub binary releases require a publisher SHA-256 digest. Bundles are
cached under `~/.cache/tools-sync` on the sending machine. Native dependencies
are supported only for the catalogued agent packages, not arbitrary npm projects.
Every candidate runs `--version` on the target before activation, so a glibc or
CPU mismatch fails without replacing the current executable. Unlike chezmoi's
bootstrap, pinned tools do not silently fall back to an older version.

Target releases/state live in `~/.local/share/tools-sync/<tool>/`. `--adopt` is
required before replacing an existing user/npm entrypoint; the original is saved
there as `original`. Existing npm package trees remain untouched, and only their
command symlinks are redirected. Subsequent versions use the recorded entrypoint.
Do not also update an adopted command with `npm install -g`: use `tools-sync`.
Brew, system packages and native Claude installations are reported and skipped;
on hp herdr remains owned by Brew. Downgrades require `--allow-downgrade`.

If herdr is running, a different version is only staged. Stop it explicitly in a
maintenance window and repeat `--apply --adopt`, then start its service. Never
restart it as a side effect of dotfile or web UI updates. `--rollback` switches
between managed releases and likewise refuses to switch a running herdr server.
After the first adoption, the original executable remains available at `original`.

Bootstrap another offline machine with the existing script first (include
`herdrServer=true` in its seed arguments when a persistent server is wanted), then run
`tools-sync <alias> --group common --tool herdr --apply`. The chezmoi installer
still tries compatible older releases, now testing a temporary candidate by its
absolute path before moving it into place.
After the first binary installation, explicitly start `herdr.service` on that
machine. Bootstrap enables the unit but leaves it stopped while the binary is absent.

### Mac web UI and remote PCs

On the designated Mac set `herdrServer = true` and `herdrWebUI = true` in chezmoi
data. `Library/LaunchAgents/local.herdr.plist` starts local herdr;
`local.herdr-web-ui.plist` independently supervises the web UI on `127.0.0.1:7317`.
These are login-user services: closing a terminal does not stop them, but logging
out or sleeping/shutting down the Mac interrupts the central UI. Remote agents
continue under their existing systemd services.

```sh
herdr-web-update                  # show the pinned and installed app
herdr-web-update --apply          # build first; restart only the web UI if loaded
herdr-web-update --prepare-bridges # prepare verified Linux runtimes without changing the app
herdr-web-update --connect        # register configured PCs through the setup API
herdr-web-update --update-bridges # explicitly approve incompatible remote bridges
```

The pinned app commit, Bun version, bridge version and desired machine aliases
are in `~/.config/herdr-web-ui/install.json`. Source/builds are in
`~/.local/share/herdr-web-ui/releases`, with `current` selecting a release and a
private Bun runtime alongside. Persistent machine registrations and credentials
stay in `~/.config/herdr-web-ui`; they are not copied between computers by chezmoi.
The chezmoi-owned `entry.ts` starts the backend without the built-in app/herdr
updaters, so browser buttons cannot bypass the explicit update process or restart
herdr. Bridge auto-updates are disabled. SSH secrets/unknown-host prompts must be
handled in Settings → Remote PCs; the command does not answer them automatically.
Both `dev` and `herdr-web-dev` omit agent and reverse port forwarding. The
dedicated work-Mac service below owns them; `IdentityAgent` still uses Skotty for
SSH authentication. `dev-mr`, `prod-mr` and `*.yandex.net` retain agent forwarding.
Logs are `~/Library/Logs/herdr.log` and `herdr-web-ui.log`. No web password/token is
configured while the UI is bound to localhost.

### Persistent work-Mac connection to dev

On a work Mac, `chezmoi apply` installs and starts `local.dev-link`. Its runner is
`~/.local/bin/dev-link`, and its launchd definition is
`~/Library/LaunchAgents/local.dev-link.plist`. It forwards Skotty's
`~/.skotty/sock/default.sock` and these remote loopback ports to the Mac:

- `127.0.0.1:2226` → `127.0.0.1:2226` (`xdg-open-svc`);
- `127.0.0.1:9222` → `127.0.0.1:9222` (browser debugging, when a browser listens).

The remote command atomically points `~/.ssh/ssh_auth_sock` at this connection's
agent socket. A real SSH command session is required; a transport-only `ssh -N`
does not initialize it. Ordinary `ssh dev`, web UI and backups do not replace the
link. The retained `~/.ssh/rc` fallback also avoids replacing an existing socket
with one from a short-lived, explicitly forwarded login.

Client liveness probes run every 3 seconds, with 2 missed probes permitted. A
12-second startup deadline also bounds DNS/authentication stalls. The runner
retries every 2 seconds and waits for Skotty if it has not started yet. Heartbeats
to the remote helper run every 2 seconds. After 12 seconds without a heartbeat,
the helper terminates only its own, non-multiplexed sshd session, releasing its
remote ports even if the client's VPN vanished without a TCP close. It verifies
the parent's owner and sshd process name first. No server-wide sshd setting or
other user's connection is changed. Recovery typically takes seconds after
network/VPN return; DNS, authentication and routing still need to work.

Logs are `~/Library/Logs/dev-link.log`; repeated identical failures are logged at
most once a minute. The connection survives terminal closure and reconnects
after sleep; it is unavailable while the Mac sleeps or is logged out. A changed
runner/plist restarts only this service on apply, preserving herdr and its agents.

```sh
launchctl print gui/$(id -u)/local.dev-link
tail -n 20 ~/Library/Logs/dev-link.log
launchctl kickstart -k gui/$(id -u)/local.dev-link
```

Remote bundles are downloaded and checked against the publisher's SHA-256, then
repacked with one reviewed `service-only-v1` policy: `remote-entry.ts` refuses to
start a herdr daemon when its socket is unavailable. This prevents bypassing the
Arc gate. The local bundle manifest has the new checksum, verified again on the
target. Its location is explicitly supplied to the web server. An upstream change
to the patched startup branch stops preparation and requires review; it never
silently returns to upstream autostart. The bundled runtime is kept on each PC;
the old standalone web-ui plugin is not needed.

### Arc startup gate (all work machines, including Mac)

`herdr-ensure-arc-mounts` checks saved native session cwd values and active legacy
herdr-history YAML records before the service starts. YAML history requires
PyYAML; if records exist and the parser is missing, startup fails closed.
`~/.config/herdr/arc-preflight.json` selects known Arc prefixes and optional
`required_paths`; registered custom mounts are also discovered with `arc mount
--list --json`. Missing ordinary cwd values block restore too.

No automatic remount, VPN change, `arc pull`, or fallback to `$HOME` is performed.
Restore mount/network access and retry the service. `arc root` plus directory
reads prove only current local accessibility: cached data can remain available
without VPN, and this gate cannot guarantee that the network stays up later.
Existing agents are never killed when a check fails. The interactive herdr guard
routes startup through the service; service ExecStart must use the real binary,
never the guard itself. Explicitly invoking an absolute vendor binary bypasses
that shell guard.

Safety regression tests: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v`.

| Task | Command |
|------|---------|
| Edit a managed file | `chezmoi edit ~/.zshrc` |
| Apply changes locally | `chezmoi apply` |
| Sync from GitHub | `chezmoi update` |
| Sync to offline VMs | `chezmoi apply` on macOS (auto-pushes via `run_push`) |
| Pull on offline VM | `chezmoi update` (triggers `run_after_pull` for oh-my-zsh etc.) |

`git.autoPush = true` on non-offline machines means `chezmoi add/edit`
auto-commits and pushes to GitHub. Offline VMs are updated on the next
`chezmoi apply` run on macOS.

## Adding a new offline machine

```sh
./chezmoi-upload-offline.sh new-host work=true hasGUI=false
```

A git remote named `new-host` is added to all managed repos. Future
`chezmoi apply` runs on macOS push to it automatically.

## Adding a new repo to offline sync

Add one stanza to `.chezmoi.toml.tmpl`:

```toml
[[data.offline_repos]]
  path = "path/relative/to/HOME"   # repo location (same on all machines)
  bare = "repo-name"               # bare repo name: ~/repo-name.git on remote
```

`./chezmoi-upload-offline.sh` and `run_push` pick it up automatically.

## Machine config (`promptBoolOnce`)

| Variable | Meaning | Offline detection |
|----------|---------|-------------------|
| `work` | Work machine (Yandex aliases, arc, etc.) | manual prompt |
| `hasGUI` | Machine has a graphical interface | manual prompt |
| `offline` | No internet (`*.yp-c.yandex.net`) | **auto** from hostname |

## dev disaster recovery on the work Mac

`dev-backup run` captures authoritative `herdr-history`, archived transcript HTML,
managed active Codex conversations (including native child threads), scoped SQLite
state, and each agent's actual Arc branch/HEAD. Dirty diffs and local checkpoints
are best effort. It never copies Arc objects, skills from junk, or reproducible
chezmoi configuration. Unmanaged/modified user units and small referenced scripts
are preserved separately. OpenCode/watchdog and unmanaged ops tabs are excluded.

Encrypted restic repository: `~/Backups/dev/repository`. Calendar LaunchAgent
`local.dev-backup` runs every 30 minutes, on login and after missed wake events.
The obsolete daily object-store job is removed on apply. No Herdr agents stop.

Use `dev-backup status`, `dev-backup snapshots`, or
`dev-backup restore --target ~/dev-recovery` (a new directory; schema-2 only).
[`RECOVERY.md`](dot_local/share/dev-backup/RECOVERY.md) is an executable runbook
for a fresh Herdr Ops agent; `prepare-recovery.py` builds its exact recovery plan
without changing existing Mac state. The key is saved as `dev-backup-restic` in
rbw and in `~/.config/dev-backup/password` (600) for unattended backups.
Tests: `python3 -m unittest discover -s tests -p test_dev_backup.py`.
