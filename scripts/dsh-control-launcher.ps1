[CmdletBinding()]
param(
    [ValidateSet('menu','discover','adopt','status','start','stop','restart','selftest','open','accept-config','watch','watchdog','watchdog-enable','watchdog-disable','help')]
    [string]$Action = 'menu',
    [ValidateRange(0,86400)][int]$WatchSeconds = 0,
    [ValidateRange(1,60)][int]$IntervalSeconds = 2,
    [string]$Distro = '', [string]$User = '', [int]$Port = 0,
    [string]$Definition = '', [string]$Instance = '', [string]$StateDirectory = '',
    [ValidateRange(20,60)][int]$TimeoutSeconds = 40,
    [switch]$Json, [switch]$NoPause, [switch]$NoOpen
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'dsh-control-detect.ps1')
$script:LastCode = 0
$script:Selected = $null
$script:Target = $null

function Show-DshResult($result) {
    if ($Json) { $result | ConvertTo-Json -Depth 16 -Compress; return }
    $labels = @{ running='可以正常使用'; stopped='已停止'; unknown='状态尚未确认'; unhealthy='服务未就绪'; observed='已完成安装发现'; not_checked='未执行检查' }
    $label = $labels[[string]$result.state]
    if (-not $label) { $label = $result.state }
    Write-Host ("DSH WSL Control 0.1.0 — {0}（{1} ms）" -f $label, $result.elapsed_ms)
    foreach ($finding in $result.findings) { Write-Host ("[{0}] {1}" -f $finding.severity, $finding.message) }
    foreach ($candidate in $result.instances) { Write-Host ("实例 {0} | DSH {1} | HOME {2} | 定义 {3}" -f $candidate.instance_id, $candidate.version, $candidate.home, $candidate.definition) }
    if ($result.instance_id) { Write-Host ("实例：" + $result.instance_id) }
    if ($result.skipped) { Write-Host '看门狗未启用或用户期望已停止，本次不启动。' }
}

function Invoke-DshAction([string]$Chosen) {
    $Chosen = $Chosen.ToLowerInvariant()
    $timer = [Diagnostics.Stopwatch]::StartNew()
    try {
        if (-not $script:Target) {
            $savedPath = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'dsh-control/target.json' } else { $null }
            if ($savedPath -and (Test-Path -LiteralPath $savedPath) -and -not $Distro) {
                $saved = Get-Content -LiteralPath $savedPath -Raw -Encoding UTF8 | ConvertFrom-Json
                $chosenUser = if ($User) { $User } else { $saved.user }
                $script:Target = Get-DshControlTarget -Distro $saved.distro -User $chosenUser
                if (-not $Instance -and (-not $User -or $User -eq $saved.user)) { $script:Selected = $saved.instance_id }
            } else { $script:Target = Get-DshControlTarget -Distro $Distro -User $User }
        }
        $request = @{ action=$Chosen; open_browser=$false }
        if ($Chosen -eq 'open') { $request.action = 'status' }
        if ($Instance) { $request.instance_id = $Instance } elseif ($script:Selected) { $request.instance_id = $script:Selected }
        if ($Definition) { $request.definition = $Definition }
        if ($Port) { $request.port = $Port }
        if ($Chosen -eq 'adopt' -and -not $Definition) { throw '接入需要 -Definition 指定 discover 返回的安装定义路径。' }
        $result = Invoke-DshCore -Target $script:Target -Request $request -TimeoutSeconds $TimeoutSeconds -StateDirectory $StateDirectory
        if ($result.ok -and $Chosen -eq 'adopt') {
            $script:Selected = $result.instance_id
            if (-not $StateDirectory -and $env:LOCALAPPDATA) {
                $saveDir = Join-Path $env:LOCALAPPDATA 'dsh-control'
                [void](New-Item -ItemType Directory -Force -Path $saveDir)
                $temp = Join-Path $saveDir ([guid]::NewGuid().ToString('N') + '.tmp')
                @{ distro=$script:Target.Distro; user=$script:Target.User; instance_id=$result.instance_id } | ConvertTo-Json | Set-Content -LiteralPath $temp -Encoding UTF8
                Move-Item -LiteralPath $temp -Destination (Join-Path $saveDir 'target.json') -Force
            }
        }
        if ($result.state -eq 'running') {
            $targetPort = if ($result.evidence.port) { $result.evidence.port } elseif ($Port) { $Port } else { 0 }
            if ($targetPort -gt 0) {
                $pathDeadline = [Math]::Min($TimeoutSeconds - 2, $timer.Elapsed.TotalSeconds + 5)
                do {
                    $endpoint = Test-DshWindowsEndpoint -Port $targetPort
                    if ($endpoint.expected_auth -or $null -ne $endpoint.http_status -or $endpoint.bind -eq 'AccessDenied') { break }
                    if ($timer.Elapsed.TotalSeconds -lt $pathDeadline) { Start-Sleep -Milliseconds 300 }
                } while ($timer.Elapsed.TotalSeconds -lt $pathDeadline)
                $result.evidence | Add-Member -NotePropertyName windows -NotePropertyValue $endpoint -Force
                if (-not $endpoint.expected_auth) {
                    $result.ok = $false
                    $result.state = 'unhealthy'
                    $result.findings += [pscustomobject]@{ code='windows_path_failed'; severity='warning'; message='WSL 后台已就绪，但 Windows 访问路径未通过检查；后台保持运行。请检查端口保留、转发或本机占用。' }
                }
            }
        }
        if ($result.ok -and ($Chosen -eq 'open' -or ($Chosen -eq 'start' -and -not $NoOpen))) {
            $remaining = [int]($TimeoutSeconds - $timer.Elapsed.TotalSeconds)
            if ($remaining -lt 16) {
                $result.ok = $false
                $result.findings += [pscustomobject]@{ code='handoff_timeout'; severity='warning'; message='后台已就绪，本轮剩余时间不足以交接浏览器；请使用仅打开。' }
            } else {
                $handoff = Invoke-DshCore -Target $script:Target -Request @{ action='open'; instance_id=$result.instance_id } -TimeoutSeconds $remaining -StateDirectory $StateDirectory
                $result.ok = $handoff.ok
                $result.findings += $handoff.findings
                $result.evidence | Add-Member -NotePropertyName browser_handoff -NotePropertyValue $handoff.ok -Force
            }
        }
        $result.action = $Chosen
        $result.elapsed_ms = $timer.ElapsedMilliseconds
        Show-DshResult $result
        $script:LastCode = if ($result.ok) { 0 } else { 1 }
    } catch {
        $result = [pscustomobject]@{ schema_version=1; ok=$false; action=$Chosen; state='unknown'; elapsed_ms=$timer.ElapsedMilliseconds; findings=@(@{ code='host_failed'; severity='warning'; message=$_.Exception.Message }); coverage=@{ runtime='unavailable' } }
        Show-DshResult $result
        $script:LastCode = 1
    }
}

if ($Action -eq 'help') {
    Write-Output 'DSH WSL Control 0.1.0: discover → adopt -Definition <WSL path> → start / open / status / stop. 多发行版使用 -Distro，多个实例使用 -Instance。-Json 输出机器协议，-NoOpen 启动时不打开浏览器。关闭菜单不会停止 DSH。'
    exit 0
}
if ($Action -eq 'menu') {
    do {
        Write-Host "`nDSH WSL Control 0.1.0 — 关闭本窗口不会停止 DSH。"
        Write-Host '[1] 启动  [2] 停止  [3] 重启  [4] 检查  [5] 仅打开界面  [6] 配置观察  [7] 监视  [8] 发现环境  [0] 退出'
        $choice = Read-Host '选择'
        $mapping = @{ '1'='start'; '2'='stop'; '3'='restart'; '4'='selftest'; '5'='open'; '6'='accept-config'; '8'='discover' }
        if ($choice -in @('2','3')) {
            Write-Host '将中断当前实例；无法判断模型任务是否正在运行。'
            if ((Read-Host '输入 YES 继续') -ne 'YES') { continue }
        }
        if ($choice -eq '7') { Write-Host '监视仅观察状态，不判断模型是否思考；Ctrl+C 退出。'; while ($true) { Invoke-DshAction 'status'; Start-Sleep -Seconds $IntervalSeconds } }
        if ($mapping.ContainsKey($choice)) { Invoke-DshAction $mapping[$choice] }
    } while ($choice -ne '0')
} elseif ($Action -eq 'watch') {
    $watchTimer = [Diagnostics.Stopwatch]::StartNew()
    $watchCode = 0
    do {
        Invoke-DshAction 'status'
        if ($script:LastCode -ne 0) { $watchCode = 1 }
        if ($WatchSeconds -gt 0 -and $watchTimer.Elapsed.TotalSeconds -ge $WatchSeconds) { break }
        Start-Sleep -Seconds $IntervalSeconds
    } while ($WatchSeconds -eq 0 -or $watchTimer.Elapsed.TotalSeconds -lt $WatchSeconds)
    $script:LastCode = $watchCode
} else { Invoke-DshAction $Action }
exit $script:LastCode
