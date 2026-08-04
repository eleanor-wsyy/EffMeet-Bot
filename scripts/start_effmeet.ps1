param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$cloudRoot = Join-Path $repoRoot "cloud_brain"
$healthUrl = "http://127.0.0.1:5000/api/health"
$dashboardUrl = "http://127.0.0.1:5000/"

function Test-EffMeetBackend {
    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        return $response.status -eq "ok"
    }
    catch {
        return $false
    }
}

if (-not (Test-EffMeetBackend)) {
    $venvPython = Join-Path $cloudRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        $python = $venvPython
    }
    else {
        $pythonCommand = Get-Command python -ErrorAction Stop
        $python = $pythonCommand.Source
    }

    $logRoot = Join-Path $cloudRoot "data\logs"
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $stdoutLog = Join-Path $logRoot "backend-$timestamp.log"
    $stderrLog = Join-Path $logRoot "backend-$timestamp-error.log"

    $backend = Start-Process `
        -FilePath $python `
        -ArgumentList @("main_brain.py") `
        -WorkingDirectory $cloudRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru

    Write-Host "Starting EffMeet backend, process ID=$($backend.Id)..."
    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        Start-Sleep -Seconds 1
        if (Test-EffMeetBackend) {
            $ready = $true
            break
        }
        if ($backend.HasExited) {
            break
        }
    }

    if (-not $ready) {
        Write-Host "Backend failed to start. Error log: $stderrLog" -ForegroundColor Red
        if (Test-Path -LiteralPath $stderrLog) {
            Get-Content -LiteralPath $stderrLog -Tail 30
        }
        if (-not $backend.HasExited) {
            Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
        }
        throw "EffMeet backend startup failed."
    }
}

if (-not $NoBrowser) {
    Write-Host "EffMeet backend is ready. Opening the experiment dashboard." -ForegroundColor Green
    Start-Process $dashboardUrl
}
else {
    Write-Host "EffMeet backend is ready. No recording has started." -ForegroundColor Green
}
