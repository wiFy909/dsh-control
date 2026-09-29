param([Parameter(ValueFromRemainingArguments=$true)][string[]]$InstallerArgs)
$ErrorActionPreference = 'Stop'
$releaseApi = 'https://api.github.com/repos/wiFy909/dsh-control/releases/tags/v0.3.4'
$PSNativeCommandUseErrorActionPreference = $false
$python = $null
$candidates = @()
$py = Get-Command py.exe -ErrorAction SilentlyContinue
if ($py) {
    $found = (& $py.Source -3.12 -c 'import sys; print(sys.executable)' 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -eq 0 -and $found) { $candidates += $found }
}
$candidates += (Join-Path $env:LOCALAPPDATA 'Programs/Python/Python312/python.exe')
foreach ($name in @('python', 'python3')) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notmatch 'WindowsApps') { $candidates += $command.Source }
}
foreach ($candidate in ($candidates | Select-Object -Unique)) {
    if (Test-Path -LiteralPath $candidate) {
        & $candidate -c 'import sys; raise SystemExit(not ((3,10) <= sys.version_info[:2] < (3,14)))' 2>$null
        if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
    }
}
$tempDir = Join-Path ([IO.Path]::GetTempPath()) ('dsh-control-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tempDir | Out-Null
try {
    if (-not $python) {
        Write-Host '正在准备 DSH Control 所需的 Python 3.12…'
        $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
        $oldUvInstall = $env:UV_UNMANAGED_INSTALL
        $oldPythonDir = $env:UV_PYTHON_INSTALL_DIR
        $oldPythonBin = $env:UV_PYTHON_BIN_DIR
        try {
            if ($uvCommand) { $uv = $uvCommand.Source }
            else {
                $env:UV_UNMANAGED_INSTALL = Join-Path $tempDir 'uv'
                $uvInstaller = Join-Path $tempDir 'uv-install.ps1'
                Invoke-WebRequest 'https://astral.sh/uv/0.12.19/install.ps1' -OutFile $uvInstaller
                & $uvInstaller
                $uv = Join-Path $env:UV_UNMANAGED_INSTALL 'uv.exe'
            }
            $env:UV_PYTHON_INSTALL_DIR = Join-Path $env:LOCALAPPDATA 'dsh-control/python'
            $env:UV_PYTHON_BIN_DIR = Join-Path $env:UV_PYTHON_INSTALL_DIR 'bin'
            & $uv python install 3.12
            if ($LASTEXITCODE -ne 0) { throw 'Python 准备失败。' }
            $python = (& $uv python find --managed-python 3.12 | Out-String).Trim()
            if ($LASTEXITCODE -ne 0) { throw 'Python 路径未找到。' }
        } finally {
            $env:UV_UNMANAGED_INSTALL = $oldUvInstall
            $env:UV_PYTHON_INSTALL_DIR = $oldPythonDir
            $env:UV_PYTHON_BIN_DIR = $oldPythonBin
        }
    }
    if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot 'MANIFEST.json'))) {
        & $python -X utf8 -I (Join-Path $PSScriptRoot 'scripts/install-control.py') --source $PSScriptRoot @InstallerArgs
        if ($LASTEXITCODE -ne 0) { throw '安装或 DSH 接入未完成，请处理上方提示后重试。' }
        return
    }
    $release = Invoke-RestMethod $releaseApi -Headers @{'User-Agent'='DSH-Control-Installer'}
    $assets = @($release.assets | Where-Object { $_.name -eq 'dsh-control-windows.zip' -and $_.state -eq 'uploaded' })
    if ($assets.Count -ne 1 -or $assets[0].digest -notmatch '^sha256:([a-f0-9]{64})$') { throw '发行包缺少有效的 SHA-256 校验记录。' }
    $expected = $Matches[1]
    $url = [string]$assets[0].browser_download_url
    if ($url -ne 'https://github.com/wiFy909/dsh-control/releases/download/v0.3.4/dsh-control-windows.zip') { throw '发行包下载地址不匹配。' }
    $archive = Join-Path $tempDir 'dsh-control-windows.zip'
    Invoke-WebRequest $url -OutFile $archive -UseBasicParsing
    if ((Get-FileHash $archive -Algorithm SHA256).Hash.ToLower() -ne $expected) { throw '下载校验失败。' }
    Expand-Archive $archive -DestinationPath (Join-Path $tempDir 'source')
    & $python -X utf8 -I (Join-Path $tempDir 'source/scripts/install-control.py') --archive $archive --sha256 $expected @InstallerArgs
    if ($LASTEXITCODE -ne 0) { throw '安装未完成，请保留上方错误信息后重试。' }
} finally { Remove-Item -LiteralPath $tempDir -Recurse -Force }
