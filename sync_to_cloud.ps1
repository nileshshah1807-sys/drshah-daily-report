# Keeps the cloud job in step with the desktop app.
#
#   .\sync_to_cloud.ps1          copy the app's current watchlist to the cloud job
#   .\sync_to_cloud.ps1 -Code    also copy the app's Python source (run after building a new version)
#
# Nothing is uploaded unless something changed. Passwords and settings are never part of this.
param([switch]$Code)

$ErrorActionPreference = 'Stop'
$repo    = $PSScriptRoot
$appDb   = 'C:\DrShah\app.db'
$source  = 'C:\Users\NILESH SHAH\OneDrive\Desktop\us stock market 4\DrShah_US_Stocks_Analysis'
$logDir  = Join-Path $repo 'logs'
$logFile = Join-Path $logDir 'sync.log'

function Write-Log($text) {
    $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $text
    Write-Output $line
    New-Item -ItemType Directory -Force $logDir | Out-Null
    Add-Content -Path $logFile -Value $line -Encoding utf8
}

try {
    $out = & python (Join-Path $repo 'tools\export_watchlist.py') $appDb (Join-Path $repo 'cloud_watchlist.json')
    if ($LASTEXITCODE -ne 0) { throw "watchlist export failed: $out" }

    if ($Code) {
        Get-ChildItem $source -File -Filter *.py | Copy-Item -Destination $repo -Force
        Copy-Item (Join-Path $source 'requirements.txt') $repo -Force
        Copy-Item (Join-Path $source 'static\*') (Join-Path $repo 'static') -Recurse -Force
    }

    git -C $repo add -A
    $changed = git -C $repo status --porcelain
    if (-not $changed) { Write-Log "nothing to upload ($out)"; exit 0 }

    $what = if ($Code) { 'Update watchlist and app source from the laptop' } else { 'Update watchlist from the laptop app' }
    git -C $repo commit --quiet -m $what
    git -C $repo push --quiet origin main
    if ($LASTEXITCODE -ne 0) { throw 'git push failed' }
    Write-Log "uploaded: $what ($(@($changed).Count) file(s) changed; $out)"
    exit 0
} catch {
    Write-Log "sync failed: $($_.Exception.Message)"
    exit 1
}
