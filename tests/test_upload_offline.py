import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = (Path(__file__).resolve().parents[1] /
          "executable_chezmoi-upload-offline.sh.tmpl").read_text()


class UploadTests(unittest.TestCase):
    def test_static_fallback_before_downgrade(self):
        function = SOURCE.split("  try_chezmoi_ver() {", 1)[1].split(
            '\n  rsh "mkdir -p ~/.local/bin"', 1)[0]
        script = """set -eu
REMOTE_OS=linux
REMOTE_ARCH=amd64
warn() { :; }
try_chezmoi_asset() { echo "$1 $2"; [ "$2" = linux-musl ]; }
try_chezmoi_ver() {""" + function + "\ntry_chezmoi_ver 2.70.3\n"
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["2.70.3 linux", "2.70.3 linux-musl"])

    def test_replaced_remote_with_stale_tracking_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "local"
            target = root / "target.git"
            def git(*args):
                return subprocess.check_output(["git", *map(str, args)], stderr=subprocess.PIPE, text=True).strip()
            git("init", "--initial-branch=main", local)
            git("-C", local, "-c", "user.name=Test", "-c", "user.email=test@example.com",
                "commit", "--allow-empty", "-m", "initial")
            git("init", "--bare", target)
            git("-C", local, "remote", "add", "dev", target)
            # Local ref describes a previous machine; the replacement is empty.
            git("-C", local, "update-ref", "refs/remotes/dev/main", "HEAD")
            function = SOURCE.split("push_repo() {", 1)[1].split(
                '\npush_repo "$DOTFILES_LOCAL"', 1)[0]
            script = """set -eu
ALIAS=dev
REMOTE_HOME=$1
step() { :; }
ok() { :; }
ssh() { shift; sh -c "$*"; }
push_repo() {""" + function + '\npush_repo "$2" target\n'
            result = subprocess.run(["bash", "-c", script, "test", str(root), str(local)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(git("-C", target, "rev-parse", "HEAD"),
                             git("-C", local, "rev-parse", "HEAD"))


if __name__ == "__main__":
    unittest.main()
