$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$startScript = Join-Path $PSScriptRoot "start_effmeet.ps1"
$statusUrl = "http://127.0.0.1:5000/api/health"
$startUrl = "http://127.0.0.1:5000/api/experiment/start"
$dashboardUrl = "http://127.0.0.1:5000/"

& $startScript -NoBrowser

$ready = $false
$health = $null
for ($attempt = 0; $attempt -lt 120; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri $statusUrl -TimeoutSec 2
        $microphoneCount = @($health.microphones).Count
        if ($health.experiment_state -ne "ready") {
            Write-Host "Backend cannot start a new experiment. Current state: $($health.experiment_state)" -ForegroundColor Red
            Start-Process $dashboardUrl
            exit 3
        }
        if (
            $microphoneCount -eq 4 -and
            $health.mqtt_connected -and
            $health.robot_online -and
            (-not $health.robot_busy)
        ) {
            $ready = $true
            break
        }
        if (($attempt % 5) -eq 0) {
            Write-Host "Waiting for readiness: microphones=$microphoneCount/4 mqtt=$($health.mqtt_connected) robot=$($health.robot_online) busy=$($health.robot_busy)"
        }
    }
    catch {
    }
    Start-Sleep -Seconds 1
}

if (-not $ready) {
    Start-Process $dashboardUrl
    throw "System was not fully ready within 120 seconds. Check the dashboard."
}

$defaultOutput = [string]$health.default_output_dir
$outputDir = Read-Host "Recording destination [$defaultOutput]"
if ([string]::IsNullOrWhiteSpace($outputDir)) {
    $outputDir = $defaultOutput
}

$groupText = Read-Host "Group number (press Enter for automatic daily increment)"
$confirmation = Read-Host "Type START to begin four-channel recording"
if ($confirmation -cne "START") {
    Write-Host "Start cancelled. The backend remains ready; no recording was started."
    Start-Process $dashboardUrl
    exit 2
}

$groupValue = $null
if (-not [string]::IsNullOrWhiteSpace($groupText)) {
    try {
        $groupValue = [int]$groupText
    }
    catch {
        throw "Group number must be an integer from 1 to 9999."
    }
    if ($groupValue -lt 1 -or $groupValue -gt 9999) {
        throw "Group number must be an integer from 1 to 9999."
    }
}
$body = @{
    output_dir = $outputDir
    group_number = $groupValue
} | ConvertTo-Json

try {
    $result = Invoke-RestMethod `
        -Method Post `
        -Uri $startUrl `
        -ContentType "application/json; charset=utf-8" `
        -Body $body `
        -TimeoutSec 30
}
catch {
    Write-Host "EXPERIMENT DID NOT START. No successful start was recorded." -ForegroundColor Red
    if ($_.ErrorDetails.Message) {
        Write-Host $_.ErrorDetails.Message -ForegroundColor Red
    }
    Start-Process $dashboardUrl
    throw
}

Write-Host "EXPERIMENT STARTED: $($result.recording.experiment_id)" -ForegroundColor Green
Write-Host "Recording destination: $($result.recording.output_dir)"
Start-Process $dashboardUrl
