param(
    [switch]$Upload,
    [string]$Port,
    [string]$Fqbn = "esp32:esp32:esp32s3",
    [string]$BuildRoot = "C:\EffMeetBuild",
    [string]$GfxLibraryPath,
    [switch]$Clean,
    [ValidateRange(1, 8)][int]$Jobs = 4,
    [int]$MonitorSeconds = 20
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$sketchRoot = Join-Path $repoRoot "robot_esp32\1.3"
$BuildRoot = [IO.Path]::GetFullPath($BuildRoot)
if ($BuildRoot -match '[^\x20-\x7E]') {
    throw "BuildRoot must be a persistent ASCII-only path, for example C:\EffMeetBuild."
}
$buildPath = Join-Path $BuildRoot "esp32s3"
$logRoot = Join-Path $BuildRoot "logs"
New-Item -ItemType Directory -Path $buildPath, $logRoot -Force | Out-Null

function Find-ArduinoCli {
    $candidates = @(
        $env:ARDUINO_CLI,
        "C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe",
        "$env:LOCALAPPDATA\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"
    ) | Where-Object { $_ }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return (Resolve-Path -LiteralPath $candidate).Path }
    }

    $shortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Arduino IDE.lnk"
    if (Test-Path -LiteralPath $shortcut) {
        $shell = New-Object -ComObject WScript.Shell
        $target = $shell.CreateShortcut($shortcut).TargetPath
        if ($target) {
            $candidate = Join-Path (Split-Path -Parent $target) "resources\app\lib\backend\resources\arduino-cli.exe"
            if (Test-Path -LiteralPath $candidate) { return (Resolve-Path -LiteralPath $candidate).Path }
        }
    }

    $command = Get-Command arduino-cli -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    throw "arduino-cli.exe was not found. Install Arduino IDE 2.x or set ARDUINO_CLI."
}

function New-ArduinoCliConfig([string]$Cli) {
    $dataPath = Join-Path $env:LOCALAPPDATA "Arduino15"
    if (-not (Test-Path -LiteralPath $dataPath)) {
        throw "Arduino data directory was not found: $dataPath"
    }
    $substDrive = ""
    if ($dataPath -match '[^\x20-\x7E]') {
        $used = @([IO.DriveInfo]::GetDrives() | ForEach-Object { $_.Name.Substring(0, 1) })
        $letter = @("Z", "Y", "X", "W", "V", "U", "T") |
            Where-Object { $_ -notin $used } | Select-Object -First 1
        if (-not $letter) { throw "No free drive letter is available for the Arduino toolchain." }
        $substDrive = "${letter}:"
        & subst.exe $substDrive $dataPath
        if ($LASTEXITCODE -ne 0) { throw "Could not map $dataPath to $substDrive" }
        $dataPath = "$substDrive\"
    }

    try {
        $configPath = Join-Path $BuildRoot "arduino-cli.yaml"
        $yamlPath = $dataPath.Replace('\', '/')
        [IO.File]::WriteAllText($configPath, "directories:`n  data: `"$yamlPath`"`n")
        & $Cli --config-file $configPath config dump | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Arduino CLI rejected $configPath" }
        return [pscustomobject]@{ Path = $configPath; SubstDrive = $substDrive }
    }
    catch {
        if ($substDrive) { & subst.exe $substDrive /D | Out-Null }
        throw
    }
}

function New-MinimalGfxLibrary([string]$Destination, [string]$RequestedSource) {
    $sourceCandidates = @(
        $RequestedSource,
        (Join-Path ([Environment]::GetFolderPath("MyDocuments")) "Arduino\libraries\GFX_Library_for_Arduino"),
        (Join-Path $env:USERPROFILE "Documents\Arduino\libraries\GFX_Library_for_Arduino")
    ) | Where-Object { $_ }
    $source = $sourceCandidates | Where-Object { Test-Path -LiteralPath (Join-Path $_ "library.properties") } | Select-Object -First 1
    if (-not $source) { throw "GFX Library for Arduino was not found. Pass -GfxLibraryPath." }
    $source = (Resolve-Path -LiteralPath $source).Path

    $destinationFull = [IO.Path]::GetFullPath($Destination)
    if (-not $destinationFull.StartsWith($BuildRoot, [StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path -Leaf $destinationFull) -ne "GFX_Library_for_Arduino") {
        throw "Unexpected generated GFX destination: $destinationFull"
    }
    $sourceRoot = Join-Path $source "src"
    $minimalSources = @(
        "Arduino_DataBus.cpp",
        "Arduino_G.cpp",
        "Arduino_GFX.cpp",
        "Arduino_TFT.cpp",
        "databus\Arduino_SWPAR8.cpp",
        "display\Arduino_ILI9488.cpp"
    )
    $fingerprintFiles = @((Join-Path $source "library.properties")) +
        @(Get-ChildItem -LiteralPath $sourceRoot -Filter "*.h" -File -Recurse | ForEach-Object FullName) +
        @($minimalSources | ForEach-Object { Join-Path $sourceRoot $_ })
    $fingerprint = "effmeet-minimal-gfx-v2`n" + (($fingerprintFiles | Sort-Object | ForEach-Object {
        "$_|$((Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash)"
    }) -join "`n")
    $fingerprintPath = Join-Path $destinationFull ".effmeet-source-fingerprint"
    if ((Test-Path -LiteralPath $fingerprintPath) -and
        (Get-Content -Raw -LiteralPath $fingerprintPath) -eq $fingerprint) {
        return $destinationFull
    }
    if (Test-Path -LiteralPath $destinationFull) {
        Remove-Item -LiteralPath $destinationFull -Recurse -Force
    }
    $destinationRoot = Join-Path $destinationFull "src"
    New-Item -ItemType Directory -Path $destinationRoot -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $source "library.properties") -Destination $destinationFull

    Get-ChildItem -LiteralPath $sourceRoot -Filter "*.h" -File -Recurse | ForEach-Object {
        $relative = $_.FullName.Substring($sourceRoot.Length).TrimStart('\')
        $target = Join-Path $destinationRoot $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $target
    }
    @'
#ifndef _ARDUINO_GFX_LIBRARY_H_
#define _ARDUINO_GFX_LIBRARY_H_
#include "Arduino_DataBus.h"
#include "databus/Arduino_SWPAR8.h"
#include "Arduino_GFX.h"
#include "Arduino_TFT.h"
#include "display/Arduino_ILI9488.h"
#endif
'@ | Set-Content -LiteralPath (Join-Path $destinationRoot "Arduino_GFX_Library.h") -Encoding utf8 -NoNewline
    $minimalSources | ForEach-Object {
        $sourceFile = Join-Path $sourceRoot $_
        if (-not (Test-Path -LiteralPath $sourceFile)) { throw "Required GFX source missing: $_" }
        $target = Join-Path $destinationRoot $_
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Copy-Item -LiteralPath $sourceFile -Destination $target
    }
    Set-Content -LiteralPath $fingerprintPath -Value $fingerprint -Encoding utf8 -NoNewline
    return $destinationFull
}

function Resolve-UploadPort([string]$RequestedPort, [string]$Cli, [string]$ConfigPath) {
    if ($RequestedPort) { return $RequestedPort }
    $boardList = (& $Cli --config-file $ConfigPath board list --json | Out-String | ConvertFrom-Json).detected_ports
    $ports = @($boardList | Where-Object { $_.port.protocol -eq "serial" } | ForEach-Object { $_.port.address })
    if ($ports.Count -eq 1) { return $ports[0] }
    throw "Upload requires -Port because serial port detection returned: $($ports -join ', ')"
}

function Capture-RestartLog([string]$SerialPort, [string]$Path, [int]$Seconds) {
    if ($Seconds -le 0) { return }
    Start-Sleep -Seconds 2
    $serial = [IO.Ports.SerialPort]::new($SerialPort, 115200)
    $serial.ReadTimeout = 500
    try {
        $serial.Open()
        $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
        while ([DateTime]::UtcNow -lt $deadline) {
            try {
                $line = $serial.ReadLine()
                $line | Tee-Object -FilePath $Path -Append
            }
            catch [TimeoutException] {}
        }
    }
    catch {
        "Serial restart log unavailable on ${SerialPort}: $($_.Exception.Message)" | Tee-Object -FilePath $Path -Append
    }
    finally {
        if ($serial.IsOpen) { $serial.Close() }
        $serial.Dispose()
    }
}

$mutex = [Threading.Mutex]::new($false, "EffMeetFirmwareBuild")
if (-not $mutex.WaitOne(0)) {
    $mutex.Dispose()
    throw "Another EffMeet firmware build/upload is already using $BuildRoot."
}

try {
    $cli = Find-ArduinoCli
    $cliConfigState = New-ArduinoCliConfig $cli
    $cliConfig = $cliConfigState.Path
    $arduinoDataDrive = $cliConfigState.SubstDrive
    $minimalGfx = New-MinimalGfxLibrary (Join-Path $BuildRoot "libraries\GFX_Library_for_Arduino") $GfxLibraryPath
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $buildLog = Join-Path $logRoot "build-$timestamp.log"
    $compileArgs = @(
        "compile", "--fqbn", $Fqbn,
        "--build-path", $buildPath,
        "--library", $minimalGfx,
        "--jobs", $Jobs, "--warnings", "default"
    )
    if ($Clean) { $compileArgs += "--clean" }
    $compileArgs += $sketchRoot

    "Arduino CLI: $cli" | Tee-Object -FilePath $buildLog
    "Arduino CLI config: $cliConfig" | Tee-Object -FilePath $buildLog -Append
    "Build path: $buildPath" | Tee-Object -FilePath $buildLog -Append
    "FQBN: $Fqbn" | Tee-Object -FilePath $buildLog -Append
    "Minimal GFX: $minimalGfx" | Tee-Object -FilePath $buildLog -Append
    "Compiler jobs: $Jobs" | Tee-Object -FilePath $buildLog -Append
    & $cli --config-file $cliConfig @compileArgs 2>&1 | Tee-Object -FilePath $buildLog -Append
    if ($LASTEXITCODE -ne 0) { throw "Firmware compile failed. See $buildLog" }
    Write-Host "Firmware compile passed. Log: $buildLog" -ForegroundColor Green

    if ($Upload) {
        $uploadPort = Resolve-UploadPort $Port $cli $cliConfig
        $uploadLog = Join-Path $logRoot "upload-$timestamp.log"
        & $cli --config-file $cliConfig upload --fqbn $Fqbn --build-path $buildPath --port $uploadPort $sketchRoot 2>&1 |
            Tee-Object -FilePath $uploadLog
        if ($LASTEXITCODE -ne 0) { throw "Firmware upload failed. See $uploadLog" }
        Write-Host "Firmware upload passed on $uploadPort. Log: $uploadLog" -ForegroundColor Green

        $restartLog = Join-Path $logRoot "restart-$timestamp.log"
        Capture-RestartLog $uploadPort $restartLog $MonitorSeconds
        Write-Host "Restart log: $restartLog" -ForegroundColor Green
    }
}
finally {
    if ($arduinoDataDrive) { & subst.exe $arduinoDataDrive /D | Out-Null }
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
