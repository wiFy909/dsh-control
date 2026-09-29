# Keep this online entry ASCII-only so a downloaded response can be piped to iex.
$ErrorActionPreference = 'Stop'
$installer = Invoke-RestMethod 'https://raw.githubusercontent.com/wiFy909/dsh-control/main/install.ps1'
& ([scriptblock]::Create($installer.TrimStart([char]0xFEFF))) @args
