# fix-encoding.ps1 (pure ASCII on purpose: GBK-safe without BOM)
# Converts project ps1 files to UTF-8 with BOM.
# Windows PowerShell 5.1 on GBK locale misreads BOM-less UTF-8 -> parse errors.
# Run this after ANY edit to project .ps1 files.
$root = Split-Path -Parent $PSScriptRoot
$files = @('scripts\build.ps1', 'scripts\ensure-node.ps1', 'start.ps1')
foreach ($f in $files) {
    $p = Join-Path $root $f
    if (Test-Path $p) {
        $c = [System.IO.File]::ReadAllText($p, (New-Object System.Text.UTF8Encoding($false)))
        $c = $c.TrimStart([char]0xFEFF)   # drop any existing leading BOM chars (prevents BOM stacking)
        [System.IO.File]::WriteAllText($p, $c, (New-Object System.Text.UTF8Encoding($true)))
        Write-Host "BOM ok: $f"
    }
}
