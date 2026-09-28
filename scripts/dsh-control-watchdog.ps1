# Compatibility entry: no independent lifecycle policy, no browser on recovery.
param([int]$Port = 0, [string]$Distro = '', [string]$User = '',
      [string]$Instance = '', [string]$StateDirectory = '',
      [ValidateRange(20,60)][int]$WaitSeconds = 40,
      [string]$LogPath = '')
& (Join-Path $PSScriptRoot 'dsh-control-launcher.ps1') -Action watchdog `
    -Distro $Distro -User $User -Port $Port -Instance $Instance `
    -StateDirectory $StateDirectory -TimeoutSeconds $WaitSeconds -NoOpen -Json
exit $LASTEXITCODE
