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

- Skipped entirely on offline machines.
- Re-runs automatically on `chezmoi apply` whenever the package list changes.
- On servers: system packages (services, monitoring) live in ansible (`~/infra`);
  this list is only for tools I run by hand (htop, mc, kubectl, ...) — brew keeps
  them user-space and out of ansible's way.
- Homebrew on Linux needs x86_64 or ARM64 with glibc (Tier 1 since brew 5.0).
  Won't work in Termux (bionic libc) — use `pkg` there.
- On Linux with glibc < 2.35 (Debian 11 and older) the script skips packages
  entirely: there are no usable bottles, brew builds the whole toolchain from
  source — that took down backup (1GB RAM) for an hour. Upgrade the OS first.

To add a package: edit `.chezmoidata/packages.toml`, run `chezmoi apply`.

## Day-to-day

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
