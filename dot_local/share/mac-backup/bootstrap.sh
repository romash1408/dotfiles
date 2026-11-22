#!/bin/sh
# Minimal bootstrap on the NEW Mac. Never starts services or applies dotfiles.
set -eu
umask 077
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$base"
/usr/bin/shasum -a 256 -c SHA256SUMS
command -v chezmoi >/dev/null
command -v python3 >/dev/null
test -x /opt/homebrew/bin/borg
test -s "$HOME/.config/chezmoi/key.txt"
test -d "$HOME/.ssh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
cat > "$tmp/chezmoi.toml" <<EOF
encryption = "age"
[age]
identity = "$HOME/.config/chezmoi/key.txt"
EOF
for file in passphrase borg-key.export borg_work_mac; do
  chezmoi --config "$tmp/chezmoi.toml" decrypt --output "$tmp/$file" "$base/$file.age"
done
install_same() {
  src=$1
  dst=$2
  mode=$3
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    cmp -s "$src" "$dst" || { echo "Refusing to replace existing $dst" >&2; exit 1; }
  else
    mkdir -p "$(dirname "$dst")"
    install -m "$mode" "$src" "$dst"
  fi
}
install_same "$tmp/passphrase" "$HOME/.config/mac-backup/passphrase" 600
install_same "$tmp/borg-key.export" "$HOME/.config/mac-backup/borg-key.export" 600
install_same "$tmp/borg_work_mac" "$HOME/.ssh/borg_work_mac" 600
install_same "$base/known_hosts" "$HOME/.config/mac-backup/known_hosts" 600
install_same "$base/config.json" "$HOME/.config/mac-backup/config.json" 600
install_same "$base/backup.py" "$HOME/.local/libexec/mac-backup/backup.py" 600
install_same "$base/ssh" "$HOME/.local/libexec/mac-backup/ssh" 700
install_same "$base/mac-backup" "$HOME/.local/bin/mac-backup" 700
echo 'Ready: ~/.local/bin/mac-backup list'
echo 'No Herdr, agent, apply or schedule was started.'
