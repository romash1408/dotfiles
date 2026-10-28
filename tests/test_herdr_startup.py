import importlib.machinery
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
UPDATE = importlib.machinery.SourceFileLoader(
    "herdr_agent_update", str(SOURCE / "dot_local/bin/executable_herdr-agent-update")
).load_module()
SYNC = importlib.machinery.SourceFileLoader(
    "startup_tools_sync", str(SOURCE / "dot_local/bin/executable_tools-sync")
).load_module()


def render(path):
    return subprocess.check_output(["chezmoi", "execute-template"],
                                   input=(SOURCE / path).read_text(), text=True, cwd=SOURCE)


@unittest.skipUnless(sys.platform == "darwin", "Mac launchd startup")
class StartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = render("dot_local/libexec/executable_herdr-server-run.tmpl")
        cls.launcher = render("dot_local/bin/executable_herdr-service-run.tmpl")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / ".local/bin").mkdir(parents=True)
        self.env = dict(os.environ, HOME=str(self.home), XPC_SERVICE_NAME="")

    def script(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nset -eu\n" + text)
        path.chmod(0o755)
        return path

    def test_failed_update_never_mounts_or_starts_server(self):
        self.script(self.home / ".local/bin/herdr-agent-update", 'echo update >> "$HOME/events"; exit 1\n')
        self.script(self.home / ".local/bin/herdr-ensure-arc-mounts", 'echo mount >> "$HOME/events"\n')
        server = self.script(self.home / "server", 'echo server >> "$HOME/events"\n')
        runner = self.script(self.home / "run", self.runner.replace("/opt/homebrew/bin/herdr", str(server)))
        result = subprocess.run([str(runner)], env=self.env, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.home / "events").read_text(), "update\n")

    def test_updates_precede_mounts_and_server_inherits_managed_settings(self):
        for tool, event in [("herdr-agent-update", "update"), ("herdr-ensure-arc-mounts", "mount")]:
            self.script(self.home / ".local/bin" / tool, f'echo {event} >> "$HOME/events"\n')
        server = self.script(self.home / "server", 'echo "server:$HERDR_MANAGED_AGENT_UPDATES:$DISABLE_AUTOUPDATER" >> "$HOME/events"\n')
        runner = self.script(self.home / "run", self.runner.replace("/opt/homebrew/bin/herdr", str(server)))
        subprocess.run([str(runner)], env=self.env, check=True, timeout=5)
        self.assertEqual((self.home / "events").read_text(), "update\nmount\nserver:1:1\n")

    def test_manual_start_loads_server_before_ui_and_never_forces_a_restart(self):
        server = self.script(self.home / "server", 'test -f "$HOME/ready" && echo "status: running"\n')
        self.script(self.home / "launchctl", '''
echo "$*" >> "$HOME/events"
case "$1" in
 print) exit 1;;
 bootstrap) case "$3" in *local.herdr.plist) touch "$HOME/ready";; esac;;
 *) exit 99;;
esac
''')
        web = self.home / ".local/share/herdr-web-ui/current/dist/index.html"
        web.parent.mkdir(parents=True)
        web.touch()
        body = self.launcher.replace("/opt/homebrew/bin/herdr", str(server))
        body = body.replace('domain="gui/', 'export PATH="$HOME:$PATH"\ndomain="gui/')
        launcher = self.script(self.home / "launcher", body)
        subprocess.run([str(launcher)], env=self.env, check=True, capture_output=True, timeout=5)
        events = (self.home / "events").read_text().splitlines()
        loads = [line for line in events if line.startswith("bootstrap ")]
        self.assertTrue(loads[0].endswith("/local.herdr.plist"), loads)
        self.assertTrue(loads[1].endswith("/local.herdr-web-ui.plist"), loads)
        self.assertNotIn("-k", " ".join(events))

    def test_live_old_launchd_job_enters_private_runner_without_recursion(self):
        self.script(self.home / ".local/libexec/herdr-server-run", 'echo private > "$HOME/events"\n')
        # Simulate launchd's direct child without starting a real launchd job.
        body = self.launcher.replace('"${PPID:-0}" = 1', '"1" = 1')
        launcher = self.script(self.home / "launcher", body)
        subprocess.run([str(launcher)], env=dict(self.env, XPC_SERVICE_NAME="local.herdr"), check=True, timeout=5)
        self.assertEqual((self.home / "events").read_text(), "private\n")


class UpdateTests(unittest.TestCase):
    def test_unknown_provider_is_rejected_without_any_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = Path(tmp) / "codex"
            entry.touch()
            with mock.patch.object(UPDATE.shutil, "which", return_value=str(entry)):
                with self.assertRaisesRegex(RuntimeError, "Unsupported agent installation"):
                    UPDATE.installations(Path(tmp))

    def test_cask_failure_stops_before_any_native_or_portable_update(self):
        plan = {"codex": {"provider": "brew-cask", "package": "codex"},
                "claude": {"provider": "claude-native", "command": "/fake/claude"}}
        with mock.patch.object(UPDATE.shutil, "which", return_value="/fake/brew"), \
                mock.patch.object(UPDATE.subprocess, "Popen") as popen:
            popen.return_value.__enter__.return_value.wait.return_value = 1
            with self.assertRaises(subprocess.CalledProcessError):
                UPDATE.update(plan, Path.home(), 1)
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(popen.call_args.args[0], ["/fake/brew", "update"])

    def test_local_tools_sync_never_uses_ssh(self):
        with mock.patch.object(SYNC, "run", return_value=subprocess.CompletedProcess([], 0, '{"ok":true}\n')) as run:
            self.assertEqual(SYNC.remote("local", {"action": "inspect", "tools": []}), {"ok": True})
            self.assertEqual(run.call_args.args[0][1], "-c")
            self.assertNotIn("ssh", run.call_args.args[0])

    def test_latest_does_not_rewrite_pinned_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / "catalog.json"
            original = json.dumps({"groups": {}, "tools": {"codex": {
                "version": "0.100.0", "kind": "npm", "package": "@openai/codex"}}})
            catalog.write_text(original)
            info = {"os": "linux", "arch": "x64", "libc": ["glibc", "2.40"],
                    "tools": {"codex": {"version": "0.200.0", "provider": "tools-sync", "path": "/fake/codex"}}}
            with mock.patch("sys.argv", ["tools-sync", "local", "--tool", "codex", "--latest", "--catalog", str(catalog)]), \
                    mock.patch.object(SYNC, "run", return_value=subprocess.CompletedProcess([], 0, '"0.200.0"')), \
                    mock.patch.object(SYNC, "remote", return_value=info):
                SYNC.main()
            self.assertEqual(catalog.read_text(), original)
