# ============================================================
# Elf Buddy 开发模式一键启动（本机调试用；分发请用 scripts\build.ps1）
# 用法：在仓库根目录执行  .\start.ps1
#   - 自动创建 venv / 装依赖（requirements.txt 变化时才重装）
#   - 弹两个窗口：elf-brain（8787）+ elf-shell（桌宠）
#   - 关闭对应窗口即停止对应端
# ============================================================
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $root 'scripts\ensure-node.ps1')

# npm 缓存放项目内（全局缓存目录可能无写权限/被杀软锁定）
$env:npm_config_cache = Join-Path $root 'tools\npm-cache'

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# ---------- 1. 大脑 ----------
Write-Step '准备 elf-brain'
$brain = Join-Path $root 'elf-brain'
$venv = Join-Path $brain '.venv'
$py = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host '创建虚拟环境...'
    python -m venv $venv
}
$marker = Join-Path $venv '.deps_hash'
$reqHash = (Get-FileHash (Join-Path $brain 'requirements.txt') -Algorithm MD5).Hash.Trim()
$curHash = if (Test-Path $marker) { (Get-Content $marker -Raw).Trim() } else { '' }
if ($reqHash -ne $curHash) {
    Write-Host '安装/更新依赖...'
    & $py -m pip install -q -r (Join-Path $brain 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw "Python 依赖安装失败（exit $LASTEXITCODE）" }
    Set-Content $marker $reqHash
} else {
    Write-Host '依赖已就绪，跳过安装'
}

Write-Step '启动 elf-brain  (http://127.0.0.1:8787/v1/profile)'
$brainUp = $false
try {
    $headers = @{}
    if ($env:ELF_TOKEN) { $headers['X-Elf-Token'] = $env:ELF_TOKEN }
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8787/v1/profile' -Headers $headers -TimeoutSec 2 -UseBasicParsing
    if ($r.StatusCode -eq 200) { $brainUp = $true }
} catch {}
if ($brainUp) {
    try {
        Invoke-WebRequest -Uri 'http://127.0.0.1:8787/v1/interest/activity/current' -Headers $headers -TimeoutSec 2 -UseBasicParsing | Out-Null
    } catch {
        throw '8787 端口运行的是旧版后端。请关闭之前的后端窗口，再重新运行 start.ps1。'
    }
    Write-Host 'brain 已在运行，跳过' -ForegroundColor Yellow
} else {
    Start-Process powershell -ArgumentList @(
        '-NoExit', '-Command',
        "Set-Location '$brain'; Write-Host 'Elf Brain 运行中，关闭本窗口即停止' -ForegroundColor Green; & '$py' run.py"
    )
}

# ---------- 2. 桌宠壳 ----------
Write-Step '准备 elf-shell'
$nodeHome = Ensure-Node
$env:Path = "$nodeHome;$env:Path"
$shell = Join-Path $root 'elf-shell'
if (-not (Test-Path (Join-Path $shell 'node_modules'))) {
    Write-Host '首次安装 npm 依赖（Electron 包较大，请耐心）...'
    if (-not $env:ELECTRON_MIRROR) { $env:ELECTRON_MIRROR = 'https://npmmirror.com/mirrors/electron/' }
    Push-Location $shell
    npm ci --no-fund --no-audit
    $npmExit = $LASTEXITCODE
    Pop-Location
    if ($npmExit -ne 0) { throw "npm ci 失败（exit $npmExit）" }
} else {
    Write-Host 'node_modules 已就绪，跳过安装'
}
& (Join-Path $root 'scripts\prepare-assets.ps1')

Write-Step '启动 elf-shell（桌宠）'
Start-Process powershell -ArgumentList @(
    '-NoExit', '-Command',
    "`$env:Path = '$nodeHome;' + `$env:Path; Set-Location '$shell'; Write-Host 'Elf Shell 运行中，关闭本窗口即停止桌宠' -ForegroundColor Green; npm run dev"
)

Write-Step '完成'
Write-Host '验证：桌面角落应出现蓝色小家伙（呼吸+眨眼，默认点击穿透，hover 可交互）'
Write-Host '立即触发一次开口（模拟进入工作间隙）：' -ForegroundColor DarkGray
Write-Host '  Invoke-RestMethod -Uri http://127.0.0.1:8787/v1/events -Method Post -ContentType ''application/json'' -Body ''{"type":"perception.idle_change","ts":0,"payload":{"idle_sec":30}}''' -ForegroundColor DarkGray
