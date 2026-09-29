"""Release publication must not expose mismatched tags or downloads."""
import hashlib
import importlib.util
import json
from pathlib import Path
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
