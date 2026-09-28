[CmdletBinding()]
param([string]$Distro='', [string]$User='', [string]$StateDirectory='', [string]$Instance='', [string]$Definition='', [switch]$Native, [switch]$PrepareOnly)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$selectionFile=Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'dsh-control/tui-target.json'
$legacySelectionFile=Join-Path $root 'TUI_TARGET.local.json'
$selectionReadFile=if (Test-Path -LiteralPath $selectionFile) { $selectionFile } elseif (Test-Path -LiteralPath $legacySelectionFile) { $legacySelectionFile } else { $selectionFile }
if (-not $Native -and -not $Distro -and -not $User -and -not $Definition -and -not $Instance -and -not $StateDirectory -and (Test-Path $selectionReadFile)) {
    $selection=Get-Content -LiteralPath $selectionReadFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($selection.purpose -ne 'dsh-control-tui-target') { throw 'TUI 目标记录无法识别，请核实本地配置。' }
    $Native=($selection.runtime -eq 'native')
    $Distro=[string]$selection.distro; $User=[string]$selection.user
    $Definition=[string]$selection.definition; $Instance=[string]$selection.instance
    $StateDirectory=[string]$selection.state_directory
}
if (-not $Native -and -not $Distro -and -not $User -and -not $Definition -and -not $Instance -and -not $StateDirectory -and -not (Test-Path $selectionReadFile)) {
    # First launch must show platform choice even when WSL and DSH are absent.
    $Native=$true
}
# Windows Terminal / ConPTY owns its viewport. RawUI sizing can advertise a
# different grid to WSL than the visible pane, corrupting cursor-addressed UI.
function Save-TuiSelection([string]$Runtime) {
    $record=@{purpose='dsh-control-tui-target';runtime=$Runtime;distro=$Distro;user=$User;definition=$Definition;instance=$Instance;state_directory=$StateDirectory}
    [void](New-Item -ItemType Directory -Path (Split-Path -Parent $selectionFile) -Force)
    $temporary=$selectionFile+'.'+[guid]::NewGuid().ToString('N')+'.tmp'
    [IO.File]::WriteAllText($temporary,($record|ConvertTo-Json),(New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination $selectionFile -Force
}
if ($Native) {
    $python=Join-Path $root '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        & py -3 -m venv (Join-Path $root '.venv')
        if ($LASTEXITCODE -ne 0) { throw '需要 Python 3.10+；请安装后重试。' }
    }
    $pipArgs=@()
    $wheels=Join-Path $root 'wheelhouse'
    if (Test-Path -LiteralPath $wheels) { $pipArgs=@('--no-index','--find-links',$wheels) }
    $stamp=(Get-FileHash (Join-Path $root 'requirements.lock') -Algorithm SHA256).Hash
    $receipt=Join-Path $root '.venv/DEPENDENCIES.sha256'
    if (-not(Test-Path $receipt) -or (Get-Content $receipt -Raw).Trim() -ne $stamp) {
        & $python -m pip install @pipArgs --require-hashes -r (Join-Path $root 'requirements.lock')
        if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，可重新运行。' }
        & $python -m pip install @pipArgs --no-build-isolation --no-deps -e $root
        if ($LASTEXITCODE -ne 0) { throw 'DSH Control 安装失败。' }
        Set-Content -LiteralPath $receipt -Value $stamp -Encoding ASCII
    }
    if ($PrepareOnly) { Write-Output $python; exit 0 }
    Save-TuiSelection 'native'
    $arguments=@('-m','dsh_control_app.app')
    if ($StateDirectory) { $arguments+=@('--state-dir',$StateDirectory) }
    if ($Instance) { $arguments+=@('--instance',$Instance) }
    if ($Definition) { $arguments+=@('--definition',$Definition) }
    if (-not $env:COLORTERM) { $env:COLORTERM='truecolor' }
    & $python @arguments
    if ($LASTEXITCODE -ne 42) { exit $LASTEXITCODE }
    $handoffFile=Join-Path $env:LOCALAPPDATA 'dsh-control/tui-handoff.json'
    if (-not(Test-Path -LiteralPath $handoffFile)) { throw 'WSL 交接记录缺失；原有 DSH 未改变。' }
    $handoff=Get-Content -LiteralPath $handoffFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($handoff.purpose -ne 'dsh-control-wsl-onboarding' -or -not $handoff.distro) { throw 'WSL 交接目标无效。' }
    $Distro=[string]$handoff.distro
    Remove-Item -LiteralPath $handoffFile
    $Native=$false
    $onboardingHandoff=$true
}
. (Join-Path $PSScriptRoot 'dsh-control-detect.ps1')
if (-not $Distro) {
    $saved=Join-Path $env:LOCALAPPDATA 'dsh-control/target.json'
    if (Test-Path -LiteralPath $saved) {
        $target=Get-Content -LiteralPath $saved -Raw | ConvertFrom-Json
        $Distro=$target.distro; $User=$target.user
        if (-not $Instance) { $Instance=$target.instance_id }
    }
}
# Resolve only the explicitly saved or the one unambiguous WSL target.
$target=if ($onboardingHandoff) { @{Distro=$Distro;User=$User} } else { Get-DshControlTarget -Distro $Distro -User $User }
$targetArgs=@('--distribution',$target.Distro)
if ($target.User) { $targetArgs+=@('--user',$target.User) }
$path=Invoke-DshProcess -File $script:DshControlWslExe -Arguments ($targetArgs+@('--exec','wslpath','-u',$root)) -TimeoutSeconds 8
if ($path.ExitCode -ne 0) { throw '无法转换 DSH Control 安装路径。' }
$linuxRoot=$path.Output.Trim()
# Install a persistent Linux-side copy: Python venvs and Unix sockets must not live on /mnt/c.
$prepareScript=$linuxRoot+'/scripts/prepare-wsl-tui.py'
Write-Host '正在准备 DSH Control 终端…'
$r=Invoke-DshProcess -File $script:DshControlWslExe -Arguments ($targetArgs+@('--exec','python3',$prepareScript,$linuxRoot)) -TimeoutSeconds 180
if ($r.ExitCode -ne 0) { throw 'TUI 运行环境准备失败；请检查 WSL 的 Python venv、pip 和网络。原有服务未改变。' }
$python=($r.Output.Trim() -split "`n")[-1].Trim()
if ($PrepareOnly) { Write-Output $python; exit 0 }
$Distro=$target.Distro; $User=$target.User
Save-TuiSelection 'wsl'
$launchArgs=$targetArgs+@('--exec','env','COLORTERM=truecolor','TERM=xterm-256color',$python,'-m','dsh_control_app.app')
$launchArgs+=@('--expected-wsl-distro',$target.Distro)
if ($target.User) { $launchArgs+=@('--expected-wsl-user',$target.User) }
if ($StateDirectory) {  $launchArgs+=@('--state-dir',$StateDirectory) }
if ($Instance) {  $launchArgs+=@('--instance',$Instance) }
if ($Definition) {  $launchArgs+=@('--definition',$Definition) }
if ($onboardingHandoff) { $launchArgs+='--onboarding-platform'; $launchArgs+='wsl' }
& $script:DshControlWslExe @launchArgs
exit $LASTEXITCODE
