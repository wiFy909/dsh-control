"""Release publication must not expose mismatched tags or downloads."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('release_check', Path(__file__).resolve().parents[1] / 'scripts/check-release.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class ReleaseDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for folder in ('assets', 'dist', '.release-governor'):
            (self.root / folder).mkdir()
        (self.root / 'pyproject.toml').write_text('version = "1.2.3"\n')
        (self.root / 'assets/RELEASE_NOTES.md').write_text('# v1.2.3\n')
        for name in ('install.sh', 'install.ps1'):
            (self.root / name).write_text('https://example.test/releases/tags/v1.2.3')
        hashes = {}
        for name in checker.ASSETS:
            (self.root / 'dist' / name).write_bytes(b'build fixture')
            hashes[name] = hashlib.sha256(b'build fixture').hexdigest()
        (self.root / '.release-governor/build-artifacts.json').write_text(json.dumps(hashes))
        self.remote = {'tag_name': 'v1.2.3', 'assets': [
            {'name': name, 'state': 'uploaded', 'digest': 'sha256:' + digest} for name, digest in hashes.items()]}

    def test_matching_build_and_uploaded_hashes(self):
        self.assertEqual(checker.check(self.root, 'v1.2.3', self.remote), 'v1.2.3')

    def test_wrong_tag_notes_and_installer_version_are_rejected(self):
        with self.assertRaises(ValueError):
            checker.check(self.root, 'v1.2.4')
        for name in ('assets/RELEASE_NOTES.md', 'install.ps1', 'install.sh'):
            path = self.root / name
            original = path.read_text()
            path.write_text(original.replace('1.2.3', '1.2.2'))
            with self.assertRaises(ValueError):
                checker.check(self.root)
            path.write_text(original)

    def test_extra_or_modified_local_files_are_rejected(self):
        extra = self.root / 'dist/SHA256SUMS'
        extra.touch()
        with self.assertRaises(ValueError):
            checker.check(self.root)
        extra.unlink()
        (self.root / 'dist/dsh-control-windows.zip').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            checker.check(self.root)

    def test_missing_or_modified_upload_is_rejected(self):
        self.remote['assets'][0]['digest'] = 'sha256:' + '0' * 64
        with self.assertRaises(ValueError):
            checker.check(self.root, published=self.remote)

        self.remote['assets'].pop()
        with self.assertRaises(ValueError):
            checker.check(self.root, published=self.remote)

    @unittest.skipUnless(os.name != 'nt' and shutil.which('bash') and shutil.which('jq'),
                         'Publication runs on Ubuntu with bash and jq')
    def test_draft_lookup_uses_release_id_before_verifying_and_publishing(self):
        source = Path(__file__).resolve().parents[1]
        (self.root / 'scripts').mkdir()
        shutil.copyfile(source / 'scripts/check-release.py', self.root / 'scripts/check-release.py')
        workflow = (source / '.github/workflows/verify.yml').read_text()
        block = workflow.split('      - name: Create draft, verify uploads and publish\n', 1)[1]
        script = '\n'.join(line[10:] for line in block.split('        run: |\n', 1)[1].splitlines())
        script = script.replace('python scripts/check-release.py', shlex.quote(sys.executable) + ' scripts/check-release.py')
        # GitHub's tag endpoint cannot resolve an unpublished draft. Model that
        # API behavior so a successful create alone cannot make this test pass.
        mock = '''gh() {
          echo "$*" >> "$MOCK_LOG"
          if [ "$1 $2" = "release view" ]; then
            if [ "$5" = "databaseId" ]; then echo 1234; return; fi
            if [ "$MOCK_STATE" = "new" ]; then return 1; fi
            if [ "$MOCK_STATE" = "public" ]; then echo '{"isDraft":false}'; else echo '{"isDraft":true}'; fi
          elif [ "$1" = "api" ]; then
            if [ "$2" != "repos/fixture/repo/releases/1234" ]; then return 44; fi
            cat "$MOCK_RESPONSE"
          elif [ "$1 $2" != "release create" ] && [ "$1 $2" != "release edit" ]; then return 90
          fi
        }
        '''
        for state, bad in [('new', False), ('draft', False), ('draft', True), ('public', False)]:
            with self.subTest(state=state, bad_digest=bad):
                data = json.loads(json.dumps(self.remote))
                if bad:
                    data['assets'][0]['digest'] = 'sha256:' + '0' * 64
                response = self.root / 'response.json'
                response.write_text(json.dumps(data))
                log = self.root / 'calls.log'
                log.write_text('')
                env = {**os.environ, 'RUNNER_TEMP': str(self.root), 'RELEASE_TAG': 'v1.2.3',
                       'GH_REPO': 'fixture/repo', 'MOCK_LOG': str(log),
                       'MOCK_RESPONSE': str(response), 'MOCK_STATE': state}
                result = subprocess.run(['bash', '-c', mock + script], cwd=self.root,
                                        env=env, capture_output=True, text=True)
                calls = log.read_text()
                if bad:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn('release edit', calls)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if state == 'public':
                        self.assertEqual(len(calls.splitlines()), 1)
                    else:
                        self.assertIn('api repos/fixture/repo/releases/1234', calls)
                        self.assertIn('release edit v1.2.3 --draft=false --latest', calls)
