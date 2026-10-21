import importlib.machinery
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
ROUTER = importlib.machinery.SourceFileLoader(
    "herdr_codex_resume", str(SOURCE / "dot_local/lib/herdr-guard/executable_codex")
).load_module()
SESSION = "01a110de-8466-7170-8975-730eeed6bbca"


class ResumeHomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def store(self, name, version=5, existing_rollout=True):
        home = self.home / name
        home.mkdir(exist_ok=True)
        rollout = home / "sessions/example.jsonl"
        if existing_rollout:
            rollout.parent.mkdir(exist_ok=True)
            rollout.write_text('{"type":"session_meta"}\n')
        with closing(sqlite3.connect(home / f"state_{version}.sqlite")) as db:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)")
            db.execute("INSERT INTO threads VALUES (?, ?)", (SESSION, str(rollout)))
            db.commit()
        return home

    def launch(self, args, **environment):
        env = {"HOME": str(self.home), "HERDR_ENV": "1", **environment}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(ROUTER.os, "execve") as execute:
            ROUTER.main(args)
            execute.assert_called_once()
            return execute.call_args.args

    def test_recovered_resume_uses_existing_home_without_changing_arguments(self):
        home = self.store(".codex-dev-recovery")
        args = ["resume", SESSION, "--model", "gpt-5.6-sol", "-c", "features.fast_mode=false"]
        real, argv, env = self.launch(args)
        self.assertEqual(real, "/opt/homebrew/bin/codex")
        self.assertEqual(argv, [real, *args])
        self.assertEqual(env["CODEX_HOME"], str(home))

    def test_isolated_resume_uses_uuid_rather_than_current_working_directory(self):
        home = self.store(".codex-herdr-DIRECT-344507")
        _, argv, env = self.launch(["resume", SESSION, "-C", str(self.home / "elsewhere")])
        self.assertEqual(env["CODEX_HOME"], str(home))
        self.assertIn(str(self.home / "elsewhere"), argv)

    def test_explicit_home_is_authoritative(self):
        self.store(".codex")
        self.store(".codex-dev-recovery")
        _, _, env = self.launch(["resume", SESSION], CODEX_HOME="/chosen/home")
        self.assertEqual(env["CODEX_HOME"], "/chosen/home")

    def test_outside_herdr_fresh_launch_and_picker_are_unchanged(self):
        self.store(".codex-dev-recovery")
        for args, extra in [(["resume", SESSION], {"HERDR_ENV": "0"}),
                            (["resume", "--last"], {}), (["resume"], {}),
                            (["--help"], {}), (["resume this task " + SESSION], {})]:
            with self.subTest(args=args):
                _, argv, env = self.launch(args, **extra)
                self.assertNotIn("CODEX_HOME", env)
                self.assertEqual(argv[1:], args)

    def test_ambiguous_copies_stop_without_launching_codex(self):
        self.store(".codex")
        self.store(".codex-dev-recovery")
        with mock.patch.dict(os.environ, {"HOME": str(self.home), "HERDR_ENV": "1"}, clear=True), \
                mock.patch.object(ROUTER.os, "execve") as execute:
            with self.assertRaisesRegex(RuntimeError, "multiple Codex homes"):
                ROUTER.main(["resume", SESSION])
            execute.assert_not_called()

    def test_unreadable_index_stops_instead_of_guessing(self):
        home = self.store(".codex-dev-recovery")
        (home / "state_5.sqlite").write_bytes(b"broken index")
        with self.assertRaisesRegex(RuntimeError, "cannot read"):
            ROUTER.resume_home(self.home, SESSION)

    def test_missing_rollout_and_unknown_uuid_do_not_redirect(self):
        self.store(".codex-dev-recovery", existing_rollout=False)
        _, _, env = self.launch(["resume", SESSION])
        self.assertNotIn("CODEX_HOME", env)
        self.assertIsNone(ROUTER.resume_home(self.home, "00000000-0000-0000-0000-000000000000"))

    def test_multiple_index_versions_in_same_home_are_not_ambiguous(self):
        home = self.store(".codex-dev-recovery", version=5)
        self.store(".codex-dev-recovery", version=6)
        self.assertEqual(ROUTER.resume_home(self.home, SESSION), home)

    def test_symlinked_isolated_homes_are_not_searched(self):
        external = self.store("other-home")
        (self.home / ".codex-herdr-external").symlink_to(external, target_is_directory=True)
        self.assertIsNone(ROUTER.resume_home(self.home, SESSION))
