import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('chezmoi') and shutil.which('kubectl'),
                     'requires chezmoi and kubectl')
class KubeConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source = root / 'source'
        self.home = root / 'home'
        self.home.mkdir()
        (self.source / '.chezmoitemplates').mkdir(parents=True)
        (self.source / 'private_dot_kube').mkdir()
        shutil.copyfile(SOURCE / 'private_dot_kube/modify_private_config',
                        self.source / 'private_dot_kube/modify_private_config')
        identity = root / 'key.txt'
        self.run_command(['chezmoi', 'age-keygen', '-o', str(identity)])
        recipient = self.run_command(['chezmoi', 'age-keygen', '-y', str(identity)]).strip()
        config = root / 'chezmoi.toml'
        config.write_text(f'encryption = "age"\n[age]\nidentity = {json.dumps(str(identity))}\n'
                          f'recipient = {json.dumps(recipient)}\n[data]\nwork = true\n')
        self.base = ['chezmoi', '-c', str(config), '-S', str(self.source),
                     '-D', str(self.home), '--persistent-state', str(root / 'state.boltdb')]
        self.personal = self.fixture('personal')
        self.work = self.fixture('work')
        for name, data in [('personal', self.personal), ('work', self.work)]:
            encrypted = self.run_command(self.base + ['encrypt'], json.dumps(data))
            (self.source / f'.chezmoitemplates/kube-{name}.json.age').write_text(encrypted)
        self.target = self.home / '.kube/config'

    @staticmethod
    def run_command(command, text=None):
        result = subprocess.run(command, input=text, text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout

    @staticmethod
    def fixture(name):
        return {'clusters': [{'name': name, 'cluster': {'server': f'https://{name}.example'}}],
                'users': [{'name': name, 'user': {'token': f'{name}-test-token'}}],
                'contexts': [{'name': name, 'context': {'cluster': name, 'user': name,
                                                       'namespace': 'initial'}}]}

    def apply(self, work=True):
        self.run_command(self.base + ['--override-data', json.dumps({'work': work}),
                                     'apply', '--force', '--parent-dirs', str(self.target)])

    def view(self):
        return json.loads(self.run_command(['kubectl', '--kubeconfig', str(self.target),
                                           'config', 'view', '--raw', '-o', 'json']))

    def assert_no_diff(self, work=True):
        diff = self.run_command(self.base + ['--override-data', json.dumps({'work': work}),
                                            'diff', str(self.target)])
        self.assertEqual(diff, '')

    def test_fresh_personal_machine_does_not_read_work_secrets(self):
        (self.source / '.chezmoitemplates/kube-work.json.age').unlink()
        self.apply(work=False)
        data = self.view()
        self.assertEqual([c['name'] for c in data['clusters']], ['personal'])
        self.assertEqual(data['current-context'], '')
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assert_no_diff(work=False)

    def test_use_context_and_namespace_do_not_create_diff(self):
        self.apply()
        for context in ['work', 'personal']:
            self.run_command(['kubectl', '--kubeconfig', str(self.target),
                              'config', 'use-context', context])
            self.run_command(['kubectl', '--kubeconfig', str(self.target),
                              'config', 'set-context', '--current', '--namespace', 'local'])
            before = self.target.read_bytes()
            self.assert_no_diff()
            self.apply()
            self.assertEqual(self.target.read_bytes(), before)

    def test_updates_managed_credentials_preserves_local_entries_and_state(self):
        data = {'kind': 'Config', 'apiVersion': 'v1', 'current-context': 'local',
                **self.fixture('local')}
        for section in ['clusters', 'users', 'contexts']:
            data[section] += self.personal[section]
        data['users'][1]['user']['token'] = 'old-test-token'
        data['contexts'][1]['context']['namespace'] = 'local-namespace'
        self.target.parent.mkdir()
        self.target.write_text(json.dumps(data))
        self.apply()
        result = self.view()
        self.assertEqual(result['current-context'], 'local')
        self.assertEqual(result['users'][1]['user']['token'], 'personal-test-token')
        self.assertEqual(result['contexts'][1]['context']['namespace'], 'local-namespace')
        for section in ['clusters', 'users', 'contexts']:
            self.assertEqual(result[section][0], data[section][0])
        self.assert_no_diff()


if __name__ == '__main__':
    unittest.main()
