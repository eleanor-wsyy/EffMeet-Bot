$ErrorActionPreference = "Stop"

$statusUrl = "http://127.0.0.1:5000/api/get_meeting_data"
$endUrl = "http://127.0.0.1:5000/api/experiment/end"
$dashboardUrl = "http://127.0.0.1:5000/"

try {
    $status = Invoke-RestMethod -Uri $statusUrl -TimeoutSec 3
}
catch {
    throw "EffMeet backend is not running. There is no active experiment to end."
}

if ($status.experiment.state -ne "recording") {
    throw "No experiment is currently recording. Current state: $($status.experiment.state)"
}

Write-Host "Active experiment: $($status.experiment.experiment_id)" -ForegroundColor Yellow
Write-Host "Elapsed seconds: $($status.experiment.elapsed_seconds)"
$confirmation = Read-Host "Type END to stop recording, verify transfer, and close the backend"
if ($confirmation -cne "END") {
    Write-Host "End cancelled. Recording continues."
    exit 2
}

Write-Host "Finalizing WAV files and verifying transfer. Do not close this window..."
try {
    $result = Invoke-RestMethod `
        -Method Post `
        -Uri $endUrl `
        -ContentType "application/json; charset=utf-8" `
        -Body "{}" `
        -TimeoutSec 180
}
catch {
    Write-Host "EXPERIMENT RECORDING STOPPED, BUT FINALIZATION WAS NOT VERIFIED." -ForegroundColor Red
    Write-Host "The backend remains running and local staging is preserved." -ForegroundColor Yellow
    if ($_.ErrorDetails.Message) {
        Write-Host $_.ErrorDetails.Message -ForegroundColor Red
    }
    Start-Process $dashboardUrl
    throw
}

Write-Host "EXPERIMENT ENDED AND VERIFIED" -ForegroundColor Green
Write-Host "Final directory: $($result.export.destination_dir)"
Start-Process -FilePath $result.export.destination_dir
