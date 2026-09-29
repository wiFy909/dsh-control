"""Exercise Windows-host decisions with a fake transport, without opening a browser."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MOCK = r'''
function Get-DshControlTarget { param($Distro,$User); return @{Distro=$Distro;User=$User} }
function Invoke-DshCore {
 param($Target,$Request,$TimeoutSeconds,$StateDirectory)
 $case=Get-Content (Join-Path $PSScriptRoot 'case.json') -Raw | ConvertFrom-Json
 $Request | ConvertTo-Json -Compress | Add-Content (Join-Path $PSScriptRoot 'calls.jsonl')
 $ok=$true; $findings=@()
 if ($Request.action -eq 'open' -and -not $case.handoff) {
  $ok=$false; $findings=@(@{code='browser_failed';severity='warning';message='Fixture browser failure'})
 }
 return [pscustomobject]@{schema_version=1; operation_id='fixture'; instance_id='0123456789abcdef'; action=$Request.action; ok=$ok; state='running'; elapsed_ms=1; findings=$findings; coverage=@{runtime='completed'}; evidence=[pscustomobject]@{port=12345;pid=999}; next_actions=@()}
}
function Test-DshWindowsEndpoint {
 param($Port)
 $case=Get-Content (Join-Path $PSScriptRoot 'case.json') -Raw | ConvertFrom-Json
 return [pscustomobject]@{http_status=$case.http;expected_auth=($case.http -eq 401);bind='not_checked'}
}
'''


@unittest.skipUnless(shutil.which('pwsh'), 'PowerShell host tests run where pwsh is installed')
class Launcher(unittest.TestCase):
    def run_case(self, action, http=401, handoff=True, no_open=False):
        with tempfile.TemporaryDirectory(prefix='dsh-host-') as tmp:
            folder = Path(tmp)
            shutil.copyfile(ROOT / 'scripts/dsh-control-launcher.ps1', folder / 'dsh-control-launcher.ps1')
            (folder / 'dsh-control-detect.ps1').write_text(MOCK, encoding='utf-8-sig')
            (folder / 'case.json').write_text(json.dumps({'http': http, 'handoff': handoff}))
            command = ['pwsh', '-NoProfile', '-File', str(folder / 'dsh-control-launcher.ps1'),
                       '-Action', action, '-Distro', 'fixture', '-User', 'fixture', '-Json']
            if no_open:
                command.append('-NoOpen')
            # Include PowerShell cold startup around the launcher's own 40 s
            # operation budget; this is a behavior test, not a startup benchmark.
            result = subprocess.run(command, capture_output=True, text=True, timeout=60,
                                    env={**os.environ, 'LOCALAPPDATA': str(folder)})
            calls = [json.loads(line) for line in (folder / 'calls.jsonl').read_text().splitlines()]
            return result.returncode, json.loads(result.stdout), calls

    def test_start_checks_windows_before_browser_handoff(self):
        code, result, calls = self.run_case('start')
        self.assertEqual(code, 0)
        self.assertEqual([c['action'] for c in calls], ['start', 'open'])
        self.assertFalse(calls[0]['open_browser'])
        self.assertTrue(result['evidence']['browser_handoff'])

    def test_windows_failure_preserves_service_and_never_opens_browser(self):
        code, result, calls = self.run_case('start', http=500)
        self.assertEqual(code, 1)
        self.assertEqual([c['action'] for c in calls], ['start'])
        self.assertEqual(result['state'], 'unhealthy')
        self.assertEqual(result['findings'][0]['code'], 'windows_path_failed')

    def test_no_open_is_respected(self):
        code, result, calls = self.run_case('start', no_open=True)
        self.assertEqual(code, 0)
        self.assertEqual([c['action'] for c in calls], ['start'])

    def test_browser_failure_does_not_stop_service(self):
        code, result, calls = self.run_case('start', handoff=False)
        self.assertEqual(code, 1)
        self.assertEqual(result['state'], 'running')
        self.assertEqual([c['action'] for c in calls], ['start', 'open'])

    def test_open_only_never_starts_or_restarts(self):
        code, result, calls = self.run_case('open')
        self.assertEqual(code, 0)
        self.assertEqual([c['action'] for c in calls], ['status', 'open'])
        self.assertEqual(result['action'], 'open')
