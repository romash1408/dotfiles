import hashlib
import importlib.machinery
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
SYNC = importlib.machinery.SourceFileLoader("tools_sync", str(SOURCE/"dot_local/bin/executable_tools-sync")).load_module()
WEB = importlib.machinery.SourceFileLoader("herdr_web_update", str(SOURCE/"dot_local/bin/executable_herdr-web-update")).load_module()


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = dict(os.environ, HOME=str(self.home))
        self.entry = self.home/".local/bin/example"
        self.entry.parent.mkdir(parents=True)
        self.entry.write_text("#!/bin/sh\necho original\n")
        self.entry.chmod(0o755)

    def tearDown(self): self.tmp.cleanup()

    def bundle(self, version, executable_version=None):
        root = self.home/("source-" + version)
        (root/"bin").mkdir(parents=True)
        entry = root/"bin/example"
        entry.write_text("#!/bin/sh\necho " + (executable_version or version) + "\n")
        entry.chmod(0o755)
        payload = self.home/(version + ".tgz")
        with tarfile.open(payload,"w:gz") as t: t.add(root/"bin",arcname="bin")
        return payload

    def install(self, version, payload, **overrides):
        request = dict(action="install", name="example", version=version,
                       sha256=hashlib.sha256(payload.read_bytes()).hexdigest(), original=str(self.entry), adopt=True)
        request.update(overrides)
        return subprocess.run([sys.executable,"-c",SYNC.WORKER,json.dumps(request)], input=payload.read_bytes(), capture_output=True, env=self.env)

    def test_bad_checksum_does_not_touch_existing_executable(self):
        result = self.install("1.0.0",self.bundle("1.0.0"),sha256="0"*64)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(self.entry.is_symlink())
        self.assertIn("original",self.entry.read_text())

    def test_incompatible_candidate_does_not_activate(self):
        result = self.install("1.0.0",self.bundle("1.0.0","broken"))
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(self.entry.is_symlink())

    def test_adoption_preserves_original_and_rollback_switches_release(self):
        for version in ("1.0.0","2.0.0"):
            result = self.install(version,self.bundle(version))
            self.assertEqual(result.returncode,0,result.stderr.decode())
        backup=self.home/".local/share/tools-sync/example/original"
        self.assertIn("original",backup.read_text())
        result=subprocess.run([sys.executable,"-c",SYNC.WORKER,json.dumps({"action":"rollback","name":"example"})],capture_output=True,env=self.env)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        self.assertEqual(subprocess.check_output([str(self.entry)],text=True).strip(),"1.0.0")

    def test_archive_traversal_is_rejected(self):
        payload=self.home/"malicious.tgz"
        with tarfile.open(payload,"w:gz") as t: t.add(self.entry,arcname="../../escape")
        result=self.install("1.0.0",payload)
        self.assertNotEqual(result.returncode,0)
        self.assertIn(b"Unsafe archive",result.stderr)
        self.assertFalse(self.entry.is_symlink())


class ArcGateTests(unittest.TestCase):
    def test_missing_yaml_reexecutes_in_existing_venv_without_bypassing_cwd_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / ".config/herdr"
            config.mkdir(parents=True)
            (config / "arc-preflight.json").write_text(json.dumps({
                "enabled": True, "arc_prefixes": [], "timeout_seconds": 1,
            }))
            history = home / "history"
            history.mkdir()
            (history / "task.yaml").write_text("lifecycle_state: active\n")
            env = dict(os.environ, HOME=tmp, XDG_CONFIG_HOME=str(home / ".config"),
                       HERDR_HISTORY_DIR=str(history))
            script = str(SOURCE / "dot_local/bin/executable_herdr-ensure-arc-mounts.tmpl")
            # -S reproduces the service's Python without the installed PyYAML.
            command = [sys.executable, "-S", script]
            missing = subprocess.run(command, env=env, capture_output=True, timeout=10)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn(b"PyYAML is missing", missing.stderr)

            fallback = home / ".local/share/dev-backup/validation-venv/bin/python3"
            fallback.parent.mkdir(parents=True)
            import shlex
            fallback.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
            fallback.chmod(0o755)
            ready = subprocess.run(command, env=env, capture_output=True, timeout=10)
            self.assertEqual(ready.returncode, 0, ready.stderr)
            self.assertIn(b"checked 0 saved cwd", ready.stdout)

            (history / "task.yaml").write_text(
                "lifecycle_state: active\nproject_cwd: " + str(home / "missing") + "\n"
            )
            fakebin = home / "bin"
            fakebin.mkdir()
            arc = fakebin / "arc"
            arc.write_text('#!/bin/sh\necho "[]"\n')
            arc.chmod(0o755)
            env["PATH"] = str(fakebin) + os.pathsep + env["PATH"]
            blocked = subprocess.run(command, env=env, capture_output=True, timeout=10)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn(b"refusing session restore", blocked.stderr)

            fallback.write_text("#!/bin/sh\nexit 1\n")
            broken = subprocess.run(command, env=env, capture_output=True, timeout=10)
            self.assertNotEqual(broken.returncode, 0)
            self.assertIn(b"PyYAML is missing", broken.stderr)

    def test_missing_saved_cwd_blocks_restore_and_empty_new_session_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)
            config=home/".config/herdr"
            config.mkdir(parents=True)
            (config/"arc-preflight.json").write_text(json.dumps({"enabled":True,"arc_prefixes":[str(home/"arcadia")],"timeout_seconds":1}))
            env=dict(os.environ,HOME=tmp,XDG_CONFIG_HOME=str(home/".config"),HERDR_HISTORY_DIR=str(home/"history"))
            fakebin=home/"bin"
            fakebin.mkdir()
            arc=fakebin/"arc"
            arc.write_text('#!/bin/sh\nif [ "$1" = mount ]; then echo "[]"; else exit 1; fi\n')
            arc.chmod(0o755)
            env["PATH"]=str(fakebin)+":"+env["PATH"]
            command=[sys.executable,str(SOURCE/"dot_local/bin/executable_herdr-ensure-arc-mounts.tmpl")]
            self.assertEqual(subprocess.run(command,env=env,capture_output=True).returncode,0)
            (config/"session.json").write_text(json.dumps({"workspaces":[{"cwd":str(home/"arcadia/project")}]}))
            failed=subprocess.run(command,env=env,capture_output=True)
            self.assertNotEqual(failed.returncode,0)
            self.assertIn(b"refusing session restore",failed.stderr)
            # An empty directory left behind by an unmounted FUSE filesystem is not safe either.
            (home/"arcadia/project").mkdir(parents=True)
            unmounted=subprocess.run(command,env=env,capture_output=True)
            self.assertNotEqual(unmounted.returncode,0)
            self.assertIn(b"refusing session restore",unmounted.stderr)


class BridgePolicyTests(unittest.TestCase):
    def test_nested_ci_executable_link_resolves_inside_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage=Path(tmp)
            member=tarfile.TarInfo('node_modules/mermaid/node_modules/.bin/katex')
            member.type=tarfile.SYMTYPE
            member.linkname='/home/runner/work/herdr-web-ui/herdr-web-ui/node_modules/mermaid/node_modules/katex/cli.js'
            WEB.relocate_ci_bin_link(member,stage)
            self.assertFalse(Path(member.linkname).is_absolute())
            self.assertEqual((stage/Path(member.name).parent/member.linkname).resolve(),
                             (stage/'node_modules/mermaid/node_modules/katex/cli.js').resolve())

    def test_ci_link_cannot_escape_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            member=tarfile.TarInfo('node_modules/.bin/bad')
            member.type=tarfile.SYMTYPE
            member.linkname='/home/runner/work/herdr-web-ui/herdr-web-ui/node_modules/../../escape'
            with self.assertRaises(RuntimeError): WEB.relocate_ci_bin_link(member,Path(tmp))

    def test_unrelated_absolute_link_is_not_relocated(self):
        with tempfile.TemporaryDirectory() as tmp:
            member=tarfile.TarInfo('node_modules/.bin/bad')
            member.type=tarfile.SYMTYPE
            member.linkname='/etc/passwd'
            WEB.relocate_ci_bin_link(member,Path(tmp))
            self.assertEqual(member.linkname,'/etc/passwd')

    def test_unknown_upstream_layout_blocks_update(self):
        with self.assertRaises(RuntimeError): WEB.service_only_entry('changed upstream entrypoint')

    def test_service_only_policy_is_idempotent(self):
        source='try { await bridgeIdentity(); } catch (error) {\nstartHerdr();\n}'
        patched=WEB.service_only_entry(source)
        self.assertEqual(WEB.service_only_entry(patched),patched)
        self.assertLess(patched.index('throw new Error'),patched.index('startHerdr'))


if __name__ == "__main__": unittest.main()
