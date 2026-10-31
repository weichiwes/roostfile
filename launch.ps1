param([switch]$Demo)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# The Microsoft Store shortcut named python.exe exits with an error until Python is installed.
function Test-Python($path) {
    $ErrorActionPreference = 'SilentlyContinue'
    & $path -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}

$python = Join-Path $root 'runtime\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    $python = if ($command -and (Test-Python $command.Source)) { $command.Source } else { $null }
}
if (-not $python) {
    Write-Host 'Python 3.12 or later is required. Nothing is downloaded automatically.'
    Read-Host 'Press Enter to close'
    exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $root '.vendor\PySide6'))) {
    Write-Host 'Dependencies are missing. From this folder, run:'
    Write-Host 'python -m pip install --only-binary=:all: --target .vendor -r requirements-ui.txt'
    Read-Host 'Press Enter to close'
    exit 1
}
$arguments = @('"' + (Join-Path $root 'desktop_app.py') + '"')
if ($Demo) { $arguments += '--demo' }
# Without a console, so nothing is left open when the window closes to the tray.
$windowed = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (Test-Path -LiteralPath $windowed) {
    Start-Process -FilePath $windowed -ArgumentList $arguments
    exit 0
}
& $python @arguments
if ($LASTEXITCODE -ne 0) {
    Read-Host 'Application stopped. Press Enter to close'
    exit $LASTEXITCODE
}
