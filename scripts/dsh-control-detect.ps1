# Shared Windows transport. Lifecycle decisions live in core/dsh_control.py.
$script:DshControlWslExe = if ($env:SystemRoot) { Join-Path $env:SystemRoot 'System32\wsl.exe' } else { 'wsl.exe' }

function ConvertTo-DshNativeArgument {
    param([AllowEmptyString()][string]$Value)
    # WSL parses option tokens before CRT unquoting: leave simple tokens bare.
    if ($Value -and $Value -notmatch '[\s"]') { return $Value }
    # CommandLineToArgvW/CRT escaping, including quotes and trailing backslashes.
    '"' + [regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
}

function Invoke-DshProcess {
    param([string]$File, [string[]]$Arguments, [string]$InputText = '',
          [int]$TimeoutSeconds = 30, [switch]$UnicodeOutput)
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $timedOut = $false
    $process = New-Object Diagnostics.Process
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $File
    $info.Arguments = (($Arguments | ForEach-Object { ConvertTo-DshNativeArgument $_ }) -join ' ')
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = if ($UnicodeOutput) { [Text.Encoding]::Unicode } else { New-Object Text.UTF8Encoding($false) }
    $info.StandardErrorEncoding = New-Object Text.UTF8Encoding($false)
    $process.StartInfo = $info
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        # Async write has a deadline too; a child that never reads stdin cannot hang us.
        $writer = New-Object IO.StreamWriter($process.StandardInput.BaseStream, (New-Object Text.UTF8Encoding($false)))
        $write = $writer.WriteAsync($InputText)
        if (-not $write.Wait([Math]::Min(5000, $TimeoutSeconds * 1000))) { $timedOut = $true; throw 'stdin_timeout' }
        $writer.Close()
        $remaining = [Math]::Max(1, $TimeoutSeconds * 1000 - [int]$timer.ElapsedMilliseconds)
        if (-not $process.WaitForExit($remaining)) { $timedOut = $true; throw 'process_timeout' }
        if (-not $stdout.Wait(1000) -or -not $stderr.Wait(1000)) { $timedOut = $true; throw 'output_timeout' }
        return [pscustomobject]@{ ExitCode = $process.ExitCode; Output = $stdout.Result; TimedOut = $false; Error = ''; ElapsedMs = $timer.ElapsedMilliseconds }
    } catch {
        try { if (-not $process.HasExited) { $process.Kill() } } catch { }
        # No raw child stderr/exception, which could contain an auth URL or config values.
        return [pscustomobject]@{ ExitCode = 1; Output = ''; TimedOut = $timedOut; Error = 'process_unavailable_or_timeout'; ElapsedMs = $timer.ElapsedMilliseconds }
    } finally { $process.Dispose() }
}

function ConvertFrom-DshWslOutput {
    param([byte[]]$Bytes)
    if (-not $Bytes) { return @() }
    $encoding = if ($Bytes.Length -ge 2 -and $Bytes[0] -eq 254 -and $Bytes[1] -eq 255) { [Text.Encoding]::BigEndianUnicode }
                elseif ($Bytes -contains 0 -or ($Bytes[0] -eq 255)) { [Text.Encoding]::Unicode }
                else { [Text.Encoding]::UTF8 }
    return @($encoding.GetString($Bytes).Trim([char]0xFEFF) -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Get-DshWslDistros {
    param([switch]$RunningOnly)
    $argsList = @('--list', '--quiet')
    if ($RunningOnly) { $argsList += '--running' }
    $result = Invoke-DshProcess -File $script:DshControlWslExe -Arguments $argsList -TimeoutSeconds 8 -UnicodeOutput
    if ($result.ExitCode -ne 0) { throw 'WSL 发行版列表读取失败，状态未知。' }
    return @($result.Output.Trim([char]0xFEFF) -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Get-DshControlTarget {
    param([string]$Distro, [string]$User)
    if (-not $Distro) {
        # Listing all distributions does not wake a stopped WSL installation.
        $names = @(Get-DshWslDistros)
        if ($names.Count -ne 1) { throw '请使用 -Distro 明确选择发行版；自动发现包括已停止的发行版，但多个目标不能猜测。' }
        $Distro = $names[0]
    }
    if ($Distro.StartsWith('-') -or $User.StartsWith('-')) { throw '目标参数无效。' }
    return [pscustomobject]@{ Distro = $Distro; User = $User }
}

function Invoke-DshCore {
    param($Target, [hashtable]$Request, [int]$TimeoutSeconds = 30, [string]$StateDirectory = '')
    $sourcePath = Join-Path (Split-Path $PSScriptRoot -Parent) 'core/dsh_control.py'
    if (-not (Test-Path -LiteralPath $sourcePath)) { throw '缺少 core/dsh_control.py，请保持 scripts 与 core 的相对目录。' }
    $source = [IO.File]::ReadAllText($sourcePath, [Text.Encoding]::UTF8)
    # Fixed Python entry; user values travel solely as JSON stdin, never shell fragments.
    $bootstrap = @'
import hashlib,json,os,pathlib,subprocess,sys,tempfile
try:
 p=json.load(sys.stdin)
 source=p['source'].encode('utf-8')
 base=(pathlib.Path(p['state_dir']).expanduser() if p.get('state_dir') else pathlib.Path.home()/'.local/share/dsh-control')/'core'
 base.mkdir(parents=True,exist_ok=True,mode=0o700)
 target=base/(hashlib.sha256(source).hexdigest()+'.py')
 if not target.exists() or target.read_bytes()!=source:
  fd,tmp=tempfile.mkstemp(dir=base)
  with os.fdopen(fd,'wb') as f: f.write(source)
  os.replace(tmp,target)
 args=[sys.executable,str(target),'request','--timeout',str(p['timeout'])]
 if p.get('state_dir'): args+=['--state-dir',p['state_dir']]
 r=subprocess.run(args,input=json.dumps(p['request']),text=True,encoding='utf-8',capture_output=True,timeout=p['timeout']+12)
 sys.stdout.write(r.stdout)
 sys.exit(r.returncode)
except Exception:
 print(json.dumps({'schema_version':1,'ok':False,'state':'unknown','findings':[{'code':'transport_failed','severity':'warning','message':'WSL helper unavailable; service state must be checked.'}],'coverage':{'runtime':'unavailable'}}))
 sys.exit(1)
'@
    $payload = @{ source = $source; request = $Request; timeout = [Math]::Max(2, $TimeoutSeconds - 15); state_dir = $StateDirectory } | ConvertTo-Json -Depth 12 -Compress
    $argsList = @('--distribution', $Target.Distro)
    if ($Target.User) { $argsList += @('--user', $Target.User) }
    $argsList += @('--exec', 'python3', '-c', $bootstrap)
    $result = Invoke-DshProcess -File $script:DshControlWslExe -Arguments $argsList -InputText $payload -TimeoutSeconds $TimeoutSeconds
    if (-not $result.Output) { throw 'WSL 调用失败或超过时限；结果未知，请重新检查，勿重复重启。' }
    try { $reply = $result.Output | ConvertFrom-Json -ErrorAction Stop } catch { throw 'WSL 返回了无效协议；未把空结果判为成功。' }
    if ($reply.schema_version -ne 1 -or $null -eq $reply.ok) { throw 'WSL 返回了不支持的协议。' }
    if ($result.ExitCode -ne 0) { $reply.ok = $false }
    return $reply
}

function Test-DshWindowsEndpoint {
    param([int]$Port)
    $request = [Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/")
    $request.Proxy = $null
    $request.Timeout = 1500
    $request.ReadWriteTimeout = 1500
    $request.AllowAutoRedirect = $false
    $response = $null
    $statusCode = $null
    try { $response = $request.GetResponse(); $statusCode = [int]$response.StatusCode }
    catch [Net.WebException] { if ($_.Exception.Response) { $response = $_.Exception.Response; $statusCode = [int]$response.StatusCode } }
    catch { }
    finally { if ($response) { $response.Close() } }
    $bind = 'not_checked'
    if ($null -eq $statusCode) {
        $probe = New-Object Net.Sockets.Socket([Net.Sockets.AddressFamily]::InterNetwork, [Net.Sockets.SocketType]::Stream, [Net.Sockets.ProtocolType]::Tcp)
        try { $probe.Bind((New-Object Net.IPEndPoint([Net.IPAddress]::Loopback, $Port))); $bind = 'available' }
        catch [Net.Sockets.SocketException] { $bind = $_.Exception.SocketErrorCode.ToString() }
        finally { $probe.Dispose() }
    }
    return [pscustomobject]@{ http_status = $statusCode; expected_auth = ($statusCode -eq 401); bind = $bind }
}
