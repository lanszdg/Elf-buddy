# ============================================================
# Elf Buddy 一键打包（产出可分发的绿色版，试用者机器零依赖）
# 用法：在仓库根目录执行  .\scripts\build.ps1 [-SkipBrain]
# 产物：elf-shell\release-new\ElfBuddy-portable-0.1.0.exe
#   - 单文件绿色版，双击即跑（自解压到临时目录运行，不写注册表）
#   - 内嵌 elf-brain.exe（免 Python）+ Electron 运行时（免 Node）
#   - 首次运行数据写入 %APPDATA%\Elf Buddy\data\
# -SkipBrain：brain.exe 已打好时跳过后端阶段（重打 shell 时用）
# ============================================================
param([switch]$SkipBrain)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'ensure-node.ps1')

# 各类缓存放项目内（全局缓存目录可能无写权限/被杀软锁定/被沙箱拦截）
$env:npm_config_cache = Join-Path $root 'tools\npm-cache'
$env:electron_config_cache = Join-Path $root 'tools\electron-cache'          # electron 包 postinstall 下载缓存
$env:ELECTRON_CACHE = Join-Path $root 'tools\electron-builder-cache'         # app-builder(Go) 读这个
$env:ELECTRON_BUILDER_CACHE = Join-Path $root 'tools\electron-builder-cache' # electron-builder(JS) 读这个
# 构建工具二进制（rcedit 等）默认从 GitHub 下载，国内走 npmmirror 镜像
$env:ELECTRON_BUILDER_BINARIES_MIRROR = 'https://npmmirror.com/mirrors/electron-builder-binaries/'
# portable 绿色版不做代码签名：跳过证书自动发现，避免下载 winCodeSign
#（其缓存内含 macOS dylib 符号链接，非管理员 Windows 解压必失败）
$env:CSC_IDENTITY_AUTO_DISCOVERY = 'false'

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# ---------- 1. 打包大脑（PyInstaller 单文件 exe） ----------
if ($SkipBrain) {
    Write-Step '1/4 跳过 elf-brain 打包（-SkipBrain）'
} else {
    Write-Step '1/4 打包 elf-brain（免 Python 依赖）'
    $brain = Join-Path $root 'elf-brain'
    $venv = Join-Path $brain '.venv'
    $py = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path $py)) {
        Write-Host '创建虚拟环境...'
        python -m venv $venv
    }
    Write-Host '安装/更新依赖（含 pyinstaller）...'
    & $py -m pip install -q -r (Join-Path $brain 'requirements.txt') pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Python 依赖安装失败（exit $LASTEXITCODE）" }
    Push-Location $brain
    & $py -m PyInstaller brain.spec --clean --noconfirm
    $brainExit = $LASTEXITCODE
    Pop-Location
    if ($brainExit -ne 0) { throw "PyInstaller 失败（exit $brainExit）" }
}
$brainExe = Join-Path $root 'elf-brain\dist\elf-brain.exe'
if (-not (Test-Path $brainExe)) { throw "brain 打包失败：$brainExe 不存在" }
Write-Host "brain 就绪: $brainExe ($([math]::Round((Get-Item $brainExe).Length/1MB,1)) MB)"

# ---------- 2. 确保 Node ----------
Write-Step '2/4 准备 Node 环境'
$nodeHome = Ensure-Node
$env:Path = "$nodeHome;$env:Path"
Write-Host "Node $(node --version)"

# ---------- 3. 安装 shell 依赖 ----------
Write-Step '3/4 安装 elf-shell 依赖'
$shell = Join-Path $root 'elf-shell'
if (-not $env:ELECTRON_MIRROR) { $env:ELECTRON_MIRROR = 'https://npmmirror.com/mirrors/electron/' }
Push-Location $shell
npm ci --no-fund --no-audit
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "npm install 失败（exit $LASTEXITCODE）" }
Pop-Location
& (Join-Path $PSScriptRoot 'prepare-assets.ps1')

# ---------- 3.5 预填 winCodeSign 缓存 ----------
# electron-builder 构建期会下载 winCodeSign-2.6.0.7z 并用 7za 解压；
# 其中 darwin/*.dylib 是符号链接，无管理员/开发者模式的 Windows 创建必失败（exit 2）。
# 绕过：预先把缓存目录填好（app-builder 检测到目录存在即跳过下载解压）。
# 7za 解到 darwin symlink 会报错，但 Windows 工具已全部解出——容忍返回码，验证 signtool 即可。
$wcCache = Join-Path $root 'tools\electron-builder-cache\winCodeSign'
$wcDone = Join-Path $wcCache 'winCodeSign-2.6.0\windows-10\x64\signtool.exe'
if (-not (Test-Path $wcDone)) {
    Write-Step '3.5/4 预填 winCodeSign 缓存（绕过符号链接权限问题）'
    New-Item -ItemType Directory -Force $wcCache | Out-Null
    $wc7z = Get-ChildItem $wcCache -Filter '*.7z' | Select-Object -First 1
    if (-not $wc7z) {
        $wc7zPath = Join-Path $wcCache 'winCodeSign-2.6.0.7z'
        Invoke-WebRequest -Uri 'https://npmmirror.com/mirrors/electron-builder-binaries/winCodeSign-2.6.0/winCodeSign-2.6.0.7z' -OutFile $wc7zPath -UseBasicParsing
        $wc7z = Get-Item $wc7zPath
    }
    $dest = Join-Path $wcCache 'winCodeSign-2.6.0'
    & (Join-Path $shell 'node_modules\7zip-bin\win\x64\7za.exe') x -y -snld -bd $wc7z.FullName "-o$dest" | Out-Null
    if (-not (Test-Path $wcDone)) { throw "winCodeSign 缓存预填失败：$wcDone 不存在" }
    Write-Host 'winCodeSign 缓存就绪（darwin 符号链接已跳过，仅 Windows 工具，本机打包够用）'
}

# ---------- 4. 打包分发版 ----------
Write-Step '4/4 electron-builder 打包绿色版'
Push-Location $shell
npm run dist
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "npm run dist 失败（exit $LASTEXITCODE）" }
Pop-Location

$artifact = Get-ChildItem (Join-Path $shell 'release-new') -Filter '*.exe' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
Write-Step '完成'
if ($artifact) {
    Write-Host "分发产物: $($artifact.FullName) ($([math]::Round($artifact.Length/1MB,1)) MB)" -ForegroundColor Green
    Write-Host '把这个 exe 发给试用者即可——双击运行，机器上不需要任何环境。'
} else {
    throw '未在 release-new/ 找到产物，请检查上方 electron-builder 日志'
}
