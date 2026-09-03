param(
    [switch]$WithMl
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$cloudRoot = Join-Path $repoRoot "cloud_brain"
$requirements = Join-Path $cloudRoot "requirements.txt"
$mlRequirements = Join-Path $cloudRoot "requirements-ml.txt"
$venvRoot = Join-Path $cloudRoot ".venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"

function Invoke-Python([string[]]$Arguments) {
    if ($script:pythonLauncher -eq "py") {
        & py @Arguments
    }
    else {
        & $script:pythonLauncher @Arguments
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Python 命令失败（退出码 $LASTEXITCODE）：$($Arguments -join ' ')"
    }
}

$pyLauncherCommand = Get-Command py -ErrorAction SilentlyContinue
if ($pyLauncherCommand) {
    $script:pythonLauncher = "py"
    $pythonVersion = (& py -3 -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}')").Trim()
    $pythonArgs = @("-3")
}
else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw "未找到 Python。请先安装 Python 3.10+，并勾选 Add Python to PATH。"
    }
    $script:pythonLauncher = $pythonCommand.Source
    $pythonVersion = (& $script:pythonLauncher -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}')").Trim()
    $pythonArgs = @()
}

try {
    $version = [version]$pythonVersion
}
catch {
    throw "无法读取 Python 版本：$pythonVersion"
}
if ($version -lt [version]"3.10") {
    throw "需要 Python 3.10 或更高版本，当前是 $pythonVersion。"
}

Write-Host "使用 Python $pythonVersion"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "创建本机虚拟环境：$venvRoot"
    if ($script:pythonLauncher -eq "py") {
        Invoke-Python (@("-3", "-m", "venv", $venvRoot))
    }
    else {
        Invoke-Python (@("-m", "venv", $venvRoot))
    }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "虚拟环境创建失败：$venvPython"
}

Write-Host "安装基础运行依赖..."
& $venvPython -m pip install -r $requirements
if ($LASTEXITCODE -ne 0) {
    throw "基础依赖安装失败。请检查网络后重试。"
}

if ($WithMl) {
    Write-Host "安装可选 AI 依赖（可能需要较长时间和较多磁盘空间）..."
    & $venvPython -m pip install -r $mlRequirements
    if ($LASTEXITCODE -ne 0) {
        throw "AI 依赖安装失败；基础录音功能仍已安装。"
    }
}

Write-Host "部署完成。后端将固定使用：$venvPython" -ForegroundColor Green
Write-Host "下一步：双击“启动实验控制台.bat”。"
if (-not $WithMl) {
    Write-Host "如需语音转写和 Silero VAD：重新运行 scripts\setup_env.ps1 -WithMl。"
}
