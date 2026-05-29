param(
    [int]$Port = 8012,
    [string]$HostAddress = "127.0.0.1",
    [switch]$AllowLan
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$ConfigPath = Join-Path $Root ".venv\pyvenv.cfg"
$Python = "python"

if (Test-Path $ConfigPath) {
    $ExecutableLine = Get-Content $ConfigPath |
        Where-Object { $_ -like "executable = *" } |
        Select-Object -First 1
    if ($ExecutableLine) {
        $Python = $ExecutableLine.Substring("executable = ".Length)
    }
}

$SitePackages = Join-Path $Root ".venv\Lib\site-packages"
$PythonPathParts = @($Root, $SitePackages)
if ($env:PYTHONPATH) {
    $PythonPathParts += $env:PYTHONPATH
}
$env:PYTHONPATH = ($PythonPathParts -join ";")

Set-Location $Root
if ($AllowLan) {
    & $Python (Join-Path $Root "app_server.py") $Port --allow-lan
} else {
    & $Python (Join-Path $Root "app_server.py") $Port --host $HostAddress
}
exit $LASTEXITCODE
