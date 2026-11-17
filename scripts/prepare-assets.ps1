param([switch]$WithSkinCutout)
$ErrorActionPreference = 'Stop'
$shell = Join-Path (Split-Path -Parent $PSScriptRoot) 'elf-shell'
$models = Join-Path $shell 'models'
New-Item -ItemType Directory -Force -Path $models | Out-Null

function Save-VerifiedAsset {
    param([string]$Name, [string]$Url, [string]$Sha256)
    $destination = Join-Path $models $Name
    if ((Test-Path -LiteralPath $destination -PathType Leaf) -and
        (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash -eq $Sha256) {
        return
    }
    $temporary = "$destination.$([guid]::NewGuid()).tmp"
    try {
        Write-Host "Downloading $Name"
        Invoke-WebRequest -Uri $Url -OutFile $temporary -UseBasicParsing -TimeoutSec 300
        if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash -ne $Sha256) {
            throw "SHA-256 verification failed for $Name"
        }
        Move-Item -LiteralPath $temporary -Destination $destination -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

Save-VerifiedAsset -Name 'pixi.min.js' -Url 'https://pixijs.download/v6.5.10/pixi.min.js' `
    -Sha256 '403F2F2EE8145FA17F60C5C89403056EFE2680E5096EC2762036486914ED19C5'

if ($WithSkinCutout) {
    $ortDist = Join-Path $shell 'node_modules/onnxruntime-web/dist'
    if (-not (Test-Path -LiteralPath $ortDist -PathType Container)) {
        throw 'Run npm ci in elf-shell before preparing skin cutout assets.'
    }
    Save-VerifiedAsset -Name 'isnet-general-use.quantized.onnx' `
        -Url 'https://huggingface.co/onnx-community/ISNet-ONNX/resolve/23a5a56796a06643a23da404d4a4b34177d546e3/onnx/model_quantized.onnx' `
        -Sha256 '9F5C5FA3CCC771612D5290A648F055F94A4FA4CE289C6B3DF7258E7E10E87A42'
    foreach ($name in @('ort.min.js', 'ort-wasm-simd.wasm')) {
        Copy-Item -LiteralPath (Join-Path $ortDist $name) -Destination (Join-Path $models $name) -Force
    }
}
