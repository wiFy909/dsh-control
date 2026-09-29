param([Parameter(ValueFromRemainingArguments=$true)][string[]]$InstallerArgs)
$ErrorActionPreference = 'Stop'
$releaseUrl = 'https://github.com/wiFy909/dsh-control/releases/download/v0.3.0'
$python = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    $command = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($command) {
        & $command.Source -c 'import sys; raise SystemExit(sys.version_info < (3,10))' 2>$null
        if ($LASTEXITCODE -eq 0) { $python = $command.Source; break }
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
        & $python -I (Join-Path $PSScriptRoot 'scripts/install-control.py') --source $PSScriptRoot @InstallerArgs
        if ($LASTEXITCODE -ne 0) { throw '安装或 DSH 接入未完成，请处理上方提示后重试。' }
        return
    }
    $archive = Join-Path $tempDir 'dsh-control.zip'
    Invoke-WebRequest "$releaseUrl/dsh-control.zip" -OutFile $archive
    $lines = (Invoke-WebRequest "$releaseUrl/SHA256SUMS").Content -split "`n"
    $matches = @($lines | Where-Object { $_ -match '^[a-f0-9]{64}  dsh-control[.]zip\s*$' })
    if ($matches.Count -ne 1) { throw '缺少发行包校验值。' }
    $expected = ($matches[0] -split '\s+')[0]
    if ((Get-FileHash $archive -Algorithm SHA256).Hash.ToLower() -ne $expected) { throw '下载校验失败。' }
    Expand-Archive $archive -DestinationPath (Join-Path $tempDir 'source')
    & $python -I (Join-Path $tempDir 'source/scripts/install-control.py') --archive $archive --sha256 $expected @InstallerArgs
    if ($LASTEXITCODE -ne 0) { throw '安装未完成，请保留上方错误信息后重试。' }
} finally { Remove-Item -LiteralPath $tempDir -Recurse -Force }
