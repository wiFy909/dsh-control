"""OS exit-code contract for the real module and installed console entrypoints."""
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CliExitTests(unittest.TestCase):
    def test_module_and_console_script_propagate_textual_exit_code(self):
        with tempfile.TemporaryDirectory(prefix='dsh-cli-exit-') as temporary:
            base = Path(temporary)
            home = base / 'home'
            home.mkdir()
            (base / 'sitecustomize.py').write_text(
                "import os\nfrom textual.app import App\n"
                "def fixture_run(self, *args, **kwargs):\n"
                "    mode = os.environ['DSH_CLI_EXIT_FIXTURE']\n"
                "    if mode == 'failure': raise RuntimeError('fixture startup failure')\n"
                "    if mode == 'no-code': return\n"
                "    self.exit(return_code=42 if mode == 'handoff' else 1 if mode == 'app-failure' else 0)\n"
                "App.run = fixture_run\n",
                encoding='utf-8',
            )
            # A fake Textual run supplies the three exit paths. The subprocess
            # still executes each real project entrypoint and returns an OS code.
            inherited = ('PATH', 'SystemRoot', 'WINDIR', 'COMSPEC', 'PATHEXT',
                         'TEMP', 'TMP', 'TMPDIR', 'USERPROFILE')
            environment = {name: os.environ[name] for name in inherited if name in os.environ}
            environment.update(HOME=str(home), USERPROFILE=str(home),
                               PYTHONPATH=os.pathsep.join((str(base), str(ROOT))))
            console = Path(sysconfig.get_path('scripts')) / ('dsh-control' + ('.exe' if os.name == 'nt' else ''))
            self.assertTrue(console.is_file(), f'console script missing: {console}')
            for entry in ((sys.executable, '-m', 'dsh_control_app.app'), (str(console),)):
                for mode, expected in (('normal', 0), ('handoff', 42),
                                       ('app-failure', 1), ('no-code', 1), ('failure', 1)):
                    with self.subTest(entry=entry, mode=mode):
                        environment['DSH_CLI_EXIT_FIXTURE'] = mode
                        result = subprocess.run(
                            [*entry, '--state-dir', str(base / (mode + '-state'))],
                            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=15)
                        self.assertEqual(result.returncode, expected, result.stderr)
                        if mode == 'failure':
                            self.assertIn('fixture startup failure', result.stderr)


if __name__ == '__main__':
    unittest.main()
