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
chezmoi init --apply git@github.com:romash1408/dotfiles.git

# 4. Set zsh as default shell
zsh_path=$(which zsh)
grep -qxF "$zsh_path" /etc/shells || sudo sh -c "echo $zsh_path >> /etc/shells"
chsh -s "$zsh_path"
```

Re-login or open a new terminal. Done.

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
  local  = "path/relative/to/HOME"   # where the repo lives on macOS
  bare   = "repo-name"               # bare repo name: ~/repo-name.git on remote
  remote = "path/relative/to/HOME"   # working dir on remote (often same as local)
```

`./chezmoi-upload-offline.sh` and `run_push` pick it up automatically.

## Machine config (`promptBoolOnce`)

| Variable | Meaning | Offline detection |
|----------|---------|-------------------|
| `work` | Work machine (Yandex aliases, arc, etc.) | manual prompt |
| `hasGUI` | Machine has a graphical interface | manual prompt |
| `offline` | No internet (`*.yp-c.yandex.net`) | **auto** from hostname |
