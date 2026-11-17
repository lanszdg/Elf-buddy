# 便携 Node 引导：确保有 Node 可用，返回 node 所在目录。
# 优先级：全局 PATH > 项目 tools/node（已引导） > 自动下载便携版到 tools/node。
# 构建机因此也零全局依赖。被 start.ps1 / scripts/build.ps1 dot-source 使用。

$script:NodeVersion = 'v22.14.0'
$script:NodeDir = Join-Path $PSScriptRoot '..\tools\node'

function Ensure-Node {
    $global = Get-Command node -ErrorAction SilentlyContinue
    if ($global) { return (Split-Path -Parent $global.Source) }

    $localExe = Join-Path $script:NodeDir 'node.exe'
    if (Test-Path $localExe) { return $script:NodeDir }

    Write-Host "未找到 Node.js，下载便携版 $script:NodeVersion 到 tools/node ..."
    $zip = Join-Path $env:TEMP "node-$script:NodeVersion-win-x64.zip"
    $urls = @(
        "https://registry.npmmirror.com/-/binary/node/$script:NodeVersion/node-$script:NodeVersion-win-x64.zip",
        "https://nodejs.org/dist/$script:NodeVersion/node-$script:NodeVersion-win-x64.zip"
    )
    $ok = $false
    foreach ($u in $urls) {
        try {
            Invoke-WebRequest -Uri $u -OutFile $zip -TimeoutSec 300 -UseBasicParsing
            $ok = $true; break
        } catch { Write-Host "源失败，换下一个: $u" -ForegroundColor Yellow }
    }
    if (-not $ok) { throw "Node 便携版下载失败（两个源均不可达），请检查网络" }

    $tmp = Join-Path $env:TEMP "node-extract-$script:NodeVersion"
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
    New-Item -ItemType Directory -Force -Path $script:NodeDir | Out-Null
    Copy-Item -Path (Join-Path $inner.FullName '*') -Destination $script:NodeDir -Recurse -Force
    Remove-Item $tmp -Recurse -Force
    Write-Host "Node $script:NodeVersion 已就绪: $script:NodeDir"
    return $script:NodeDir
}
