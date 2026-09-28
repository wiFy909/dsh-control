[CmdletBinding()]
param(
    [ValidateSet('tui','menu','start','open','status','selftest','stop','restart')][string]$Action = 'tui',
    [switch]$NoOpen, [switch]$Json
)
$ErrorActionPreference = 'Stop'
try {
    $config = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'test-target.local.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($config.purpose -ne 'dsh-control-manual-test-v1' -or -not $config.instance_id -or -not $config.state_directory -or -not $config.distro -or -not $config.user) {
        throw '测试目标配置不完整，请重新配置测试入口。'
    }
    if ($Action -eq 'tui') {
        & (Join-Path $PSScriptRoot 'start-dsh-control-tui.ps1') -Distro $config.distro -User $config.user -Instance $config.instance_id -StateDirectory $config.state_directory
        exit $LASTEXITCODE
    }
    if ($Action -eq 'menu') {
        $Host.UI.RawUI.WindowTitle = 'DSH Control（测试）'
        Write-Host 'DSH Control — 独立测试环境' -ForegroundColor Cyan
        Write-Host '原来的 ROG DSH 继续用于日常工作。这里不会显示原有会话。'
        Write-Host '第一次按 1 打开；关闭网页后按 5 重开；测试结束按 2 停止。'
        Write-Host '本轮不用填 API Key，也不用发送模型消息。'
        Write-Host ('测试端口：' + $config.port)
    }
    & (Join-Path $PSScriptRoot 'dsh-control-launcher.ps1') -Action $Action `
        -Distro $config.distro -User $config.user -Instance $config.instance_id `
        -StateDirectory $config.state_directory -Port $config.port `
        -TimeoutSeconds 60 -NoOpen:$NoOpen -Json:$Json
    exit $LASTEXITCODE
} catch {
    Write-Host ('无法打开测试入口：' + $_.Exception.Message) -ForegroundColor Red
    if ($Action -eq 'menu') { [void](Read-Host '按 Enter 关闭') }
    exit 1
}
