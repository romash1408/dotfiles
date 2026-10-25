import runpy
import tarfile
import tempfile
import unittest
from pathlib import Path


relocate = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                              'dot_local/bin/executable_herdr-web-update'))['relocate_ci_bin_link']


class BundleLinksTest(unittest.TestCase):
    def link(self, name, target):
        member = tarfile.TarInfo(name)
        member.type = tarfile.SYMTYPE
        member.linkname = target
        return member

    def test_known_ci_paths_resolve_inside_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            for project in ('herdr-web-ui', 'ovis'):
                for name in ('./node_modules/.bin/tsc',
                             './node_modules/example/node_modules/.bin/tsc'):
                    with self.subTest(project=project, name=name):
                        member = self.link(name, f'/home/runner/work/{project}/{project}/node_modules/typescript/bin/tsc')
                        relocate(member, stage)
                        self.assertFalse(Path(member.linkname).is_absolute())
                        self.assertEqual((stage / name).parent.joinpath(member.linkname).resolve(),
                                         (stage / 'node_modules/typescript/bin/tsc').resolve())

    def test_unknown_absolute_path_is_not_whitelisted(self):
        member = self.link('./node_modules/.bin/tsc', '/home/runner/work/other/other/node_modules/typescript/bin/tsc')
        with tempfile.TemporaryDirectory() as directory:
            relocate(member, Path(directory))
        self.assertTrue(Path(member.linkname).is_absolute())

    def test_target_cannot_escape_using_known_prefix(self):
        member = self.link('./node_modules/.bin/tsc', '/home/runner/work/ovis/ovis/node_modules/../../outside')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'Unsafe upstream archive link'):
                relocate(member, Path(directory))

    def test_member_path_cannot_escape_bundle(self):
        member = self.link('node_modules/../../outside/.bin/tsc', '/home/runner/work/ovis/ovis/node_modules/typescript/bin/tsc')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'Unsafe upstream archive link'):
                relocate(member, Path(directory))


if __name__ == '__main__':
    unittest.main()
