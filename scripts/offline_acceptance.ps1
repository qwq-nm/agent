$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment not found: $python"
}

$baseTemp = Join-Path $env:TEMP "secagent-x-offline-$PID"
& $python -m pytest backend/tests/unit/test_config_secrets.py -v --basetemp $baseTemp
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $python -m pytest backend/tests --cov=secagent --cov-report=term-missing --cov-fail-under=85 --basetemp $baseTemp
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if ($null -eq $npmCommand) {
    $npmCommand = Get-Command npm -ErrorAction Stop
}
Push-Location (Join-Path $root "frontend")
try {
    & $npmCommand.Source test -- --run
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $npmCommand.Source run build
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    Pop-Location
}

& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "scripts\check_no_secrets.ps1")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "offline acceptance: passed"
