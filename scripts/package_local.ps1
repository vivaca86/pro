param(
    [string]$OutputDir,
    [string]$PackageName = "pro-local-runner"
)

$ErrorActionPreference = "Stop"
$RootPath = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $OutputDir) {
    $OutputDir = Join-Path $RootPath "dist"
}

function Assert-Under {
    param(
        [string]$Path,
        [string]$Parent,
        [string]$Label
    )

    $resolvedPath = if (Test-Path $Path) {
        (Resolve-Path $Path).Path
    } else {
        $existingParent = Split-Path -Parent $Path
        $leaf = Split-Path -Leaf $Path
        Join-Path (Resolve-Path $existingParent).Path $leaf
    }

    $resolvedParent = (Resolve-Path $Parent).Path
    if (-not $resolvedPath.StartsWith($resolvedParent, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must stay under $resolvedParent"
    }
}

$OutputRoot = if (Test-Path $OutputDir) {
    (Resolve-Path $OutputDir).Path
} else {
    (New-Item -ItemType Directory -Path $OutputDir -Force).FullName
}
Assert-Under -Path $OutputRoot -Parent $RootPath -Label "Output directory"

$Stage = Join-Path $OutputRoot "$PackageName-stage"
$ZipPath = Join-Path $OutputRoot "$PackageName.zip"
Assert-Under -Path $Stage -Parent $OutputRoot -Label "Stage directory"
Assert-Under -Path $ZipPath -Parent $OutputRoot -Label "Zip path"

if (Test-Path $Stage) {
    Remove-Item -LiteralPath $Stage -Recurse -Force
}
New-Item -ItemType Directory -Path $Stage -Force | Out-Null

$include = @(
    "app_server.py",
    "index.html",
    "requirements.txt",
    "pyproject.toml",
    "README.md",
    "LOCAL_RUN.md",
    "start-local.bat",
    "assets",
    "examples",
    "game_data_engine",
    "scripts"
)

foreach ($item in $include) {
    $source = Join-Path $RootPath $item
    if (-not (Test-Path $source)) {
        throw "Missing package item: $item"
    }
    Copy-Item -LiteralPath $source -Destination (Join-Path $Stage $item) -Recurse -Force
}

Get-ChildItem -LiteralPath $Stage -Recurse -Force -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $Stage -Recurse -Force -File |
    Where-Object { $_.Extension -in @(".pyc", ".pyo") } |
    Remove-Item -Force

if (Test-Path $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $Stage,
    $ZipPath,
    [System.IO.Compression.CompressionLevel]::Optimal,
    $false
)

if (-not (Test-Path $ZipPath)) {
    throw "Package zip was not created: $ZipPath"
}

Remove-Item -LiteralPath $Stage -Recurse -Force

if (-not (Test-Path $ZipPath)) {
    throw "Package zip was removed unexpectedly: $ZipPath"
}

Write-Host "Created local runner package:"
Write-Host $ZipPath
