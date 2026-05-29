param(
    [int]$Port = 8012,
    [switch]$AllowLan,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvDir = Join-Path $Root ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = Join-Path $Root "requirements.txt"
$RequirementsHash = Join-Path $VenvDir ".requirements.sha256"
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$SetupLog = Join-Path $LogDir ("setup-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$TranscriptStarted = $false

try {
    Start-Transcript -Path $SetupLog -Append | Out-Null
    $TranscriptStarted = $true
} catch {
    $TranscriptStarted = $false
}

trap {
    Write-Host ""
    Write-Host "Local dashboard setup failed:"
    Write-Host $_.Exception.Message
    Write-Host ""
    Write-Host "Setup log:"
    Write-Host $SetupLog
    if (Test-Path $SetupLog) {
        Write-Host ""
        Write-Host "Last setup log lines:"
        Get-Content -LiteralPath $SetupLog -Tail 40
    }
    if ($TranscriptStarted) {
        try { Stop-Transcript | Out-Null } catch {}
    }
    exit 1
}

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message"
}

function Test-PythonCandidate {
    param(
        [string]$Executable,
        [string[]]$Arguments = @()
    )

    $command = Get-Command $Executable -ErrorAction SilentlyContinue
    if (-not $command) {
        return $null
    }

    $versionCheck = "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Executable @Arguments -c $versionCheck 1> $null 2> $null
        $exitCode = $LASTEXITCODE
    } catch {
        $exitCode = 1
    } finally {
        $ErrorActionPreference = $previousPreference
    }

    if ($exitCode -eq 0) {
        return @{
            Executable = $Executable
            Arguments = $Arguments
        }
    }

    return $null
}

function Find-Python {
    $candidates = @(
        @{ Executable = (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"); Arguments = @() },
        @{ Executable = (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"); Arguments = @() },
        @{ Executable = (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"); Arguments = @() },
        @{ Executable = (Join-Path $env:ProgramFiles "Python313\python.exe"); Arguments = @() },
        @{ Executable = (Join-Path $env:ProgramFiles "Python312\python.exe"); Arguments = @() },
        @{ Executable = (Join-Path $env:ProgramFiles "Python311\python.exe"); Arguments = @() },
        @{ Executable = "py"; Arguments = @("-3.12") },
        @{ Executable = "py"; Arguments = @("-3.11") },
        @{ Executable = "py"; Arguments = @("-3") },
        @{ Executable = "python"; Arguments = @() },
        @{ Executable = "python3"; Arguments = @() }
    )

    foreach ($candidate in $candidates) {
        $result = Test-PythonCandidate -Executable $candidate.Executable -Arguments $candidate.Arguments
        if ($result) {
            return $result
        }
    }

    return $null
}

function Install-PythonWithWinget {
    $winget = Get-Command "winget" -ErrorAction SilentlyContinue
    if (-not $winget) {
        return $false
    }

    Write-Step "Python 3.12 was not found. Installing with winget"
    & winget install --id Python.Python.3.12 --exact --scope user --accept-package-agreements --accept-source-agreements
    return $LASTEXITCODE -eq 0
}

function Ensure-Venv {
    if (Test-Path $VenvPython) {
        return
    }

    Write-Step "Creating local Python environment"
    $python = Find-Python
    if (-not $python) {
        Install-PythonWithWinget | Out-Null
        $python = Find-Python
    }

    if (-not $python) {
        Write-Host "Python 3.11 or newer is required."
        Write-Host "Install it from https://www.python.org/downloads/windows/ and run start-local.bat again."
        Start-Process "https://www.python.org/downloads/windows/"
        throw "Python 3.11+ was not found."
    }

    & $python.Executable @($python.Arguments) -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) {
        throw "Failed to create .venv."
    }
}

function Ensure-Dependencies {
    $currentHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Requirements).Hash
    $savedHash = if (Test-Path $RequirementsHash) { (Get-Content -Raw $RequirementsHash).Trim() } else { "" }

    if ($currentHash -eq $savedHash) {
        Write-Step "Dependencies are already installed"
        return
    }

    Write-Step "Installing dependencies"
    & $VenvPython -m pip install --disable-pip-version-check -r $Requirements
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed."
    }

    Set-Content -LiteralPath $RequirementsHash -Value $currentHash -Encoding ASCII
}

Set-Location $Root
Ensure-Venv
Ensure-Dependencies

$env:APP_DATA_DIR = Join-Path $Root "data"
$env:APP_OUTPUT_DIR = Join-Path $Root "data\output"

Write-Step "Starting local dashboard server"
if ($AllowLan) {
    & (Join-Path $PSScriptRoot "start_server.ps1") -Port $Port -HostAddress "127.0.0.1" -AllowLan -LogDir $LogDir
} else {
    & (Join-Path $PSScriptRoot "start_server.ps1") -Port $Port -HostAddress "127.0.0.1" -LogDir $LogDir
}
if ($LASTEXITCODE -ne 0) {
    throw "Local server failed to start."
}

$hostName = if ($AllowLan) { "127.0.0.1" } else { "127.0.0.1" }
$url = "http://${hostName}:$Port/index.html"

if (-not $NoBrowser) {
    Start-Process $url
}

Write-Host ""
Write-Host "Local dashboard is running:"
Write-Host $url
Write-Host ""
Write-Host "To stop it later, run:"
Write-Host "powershell -ExecutionPolicy Bypass -File scripts\stop_server.ps1 -Port $Port"
Write-Host ""
Write-Host "Logs are stored in:"
Write-Host $LogDir

if ($TranscriptStarted) {
    try { Stop-Transcript | Out-Null } catch {}
}
