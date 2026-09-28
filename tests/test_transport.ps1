param([string]$Python = 'python3', [string]$Distro = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../scripts/dsh-control-detect.ps1')
$passed = 0
function Assert($Condition, [string]$Message) { if (-not $Condition) { throw $Message }; $script:passed++ }
$values = @('plain', '', 'a b', '中文目录', 'a"quoted"b', 'ends\', 'a\"b', '%PATH% & whoami', '$HOME; $(whoami)')
$arguments = @('-X', 'utf8', '-c', 'import json,sys;print(json.dumps(sys.argv[1:],ensure_ascii=False))') + $values
$result = Invoke-DshProcess -File $Python -Arguments $arguments -TimeoutSeconds 5
Assert ($result.ExitCode -eq 0) 'argv child failed'
$decoded = ConvertFrom-Json -InputObject $result.Output
Assert ($decoded.Count -eq $values.Count) 'argv count mismatch'
for ($i=0; $i -lt $values.Count; $i++) { Assert ($decoded[$i] -ceq $values[$i]) "argv mismatch $i" }
$payload = '{"home":"/home/中文 名称","value":"a\"b"}'
$result = Invoke-DshProcess -File $Python -Arguments @('-c','import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())') -InputText $payload -TimeoutSeconds 5
Assert ($result.Output -ceq $payload) 'stdin Unicode mismatch'
$result = Invoke-DshProcess -File $Python -Arguments @('-c','import sys;sys.exit(7)') -TimeoutSeconds 5
Assert ($result.ExitCode -eq 7) 'exit code lost'
$result = Invoke-DshProcess -File $Python -Arguments @('-c','import time;time.sleep(10)') -TimeoutSeconds 1
Assert ($result.TimedOut -and $result.ElapsedMs -lt 3000) 'timeout not bounded'
$longInput = 'x' * 1000000
$result = Invoke-DshProcess -File $Python -Arguments @('-c','import time;time.sleep(10)') -InputText $longInput -TimeoutSeconds 1
Assert ($result.TimedOut -and $result.ElapsedMs -lt 3000) 'stdin timeout not bounded'
$utf16 = [Text.Encoding]::Unicode.GetBytes("Ubuntu 中文`nDebian")
$names = @(ConvertFrom-DshWslOutput $utf16)
Assert ($names.Count -eq 2 -and $names[0] -eq 'Ubuntu 中文') 'distro decoding failed'
if ($Distro) {
    $result = Invoke-DshProcess -File $script:DshControlWslExe -Arguments @('--distribution', $Distro, '--exec', 'python3', '-c', 'import json,sys;print(json.dumps(json.load(sys.stdin),ensure_ascii=False))') -InputText $payload -TimeoutSeconds 8
    Assert ($result.ExitCode -eq 0) 'WSL option or Python-code argument corrupted'
    $parsed = ConvertFrom-Json -InputObject $result.Output
    Assert ($parsed.home -ceq '/home/中文 名称') 'WSL stdin Unicode corrupted'
    $names = @(Get-DshWslDistros -RunningOnly)
    Assert ($names -contains $Distro) 'WSL UTF-16 running-list decoding failed'
}
$script:DshControlWslExe = 'not-an-installed-program-dsh-fixture'
$failed = $false
try { Get-DshWslDistros | Out-Null } catch { $failed = $true }
Assert $failed 'WSL failure became empty success'
[pscustomobject]@{ tests=$passed; result='PASS'; powershell=$PSVersionTable.PSVersion.ToString() } | ConvertTo-Json -Compress
