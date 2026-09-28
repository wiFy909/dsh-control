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
if (-not $python) { throw '请先从 https://www.python.org/downloads/windows/ 安装 Python 3.10+，再重新运行本命令。' }
$tempDir = Join-Path ([IO.Path]::GetTempPath()) ('dsh-control-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tempDir | Out-Null
try {
    $archive = Join-Path $tempDir 'dsh-control.zip'
    Invoke-WebRequest "$releaseUrl/dsh-control.zip" -OutFile $archive
    $lines = (Invoke-WebRequest "$releaseUrl/SHA256SUMS").Content -split "`n"
    $matches = @($lines | Where-Object { $_ -match '^[a-f0-9]{64}  dsh-control[.]zip\s*$' })
    if ($matches.Count -ne 1) { throw '缺少发行包校验值。' }
    $expected = ($matches[0] -split '\s+')[0]
    if ((Get-FileHash $archive -Algorithm SHA256).Hash.ToLower() -ne $expected) { throw '下载校验失败。' }
    Expand-Archive $archive -DestinationPath (Join-Path $tempDir 'source')
    & $python -I (Join-Path $tempDir 'source/scripts/install-control.py') --archive $archive --sha256 $expected
    if ($LASTEXITCODE -ne 0) { throw '安装未完成，请保留上方错误信息后重试。' }
} finally { Remove-Item -LiteralPath $tempDir -Recurse -Force }
