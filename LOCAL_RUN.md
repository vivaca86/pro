# Local runner

Use this when the dashboard should run on a Windows PC without a central server.

## Start

1. Unzip the package.
2. Double-click `start-local.bat`.
3. Wait for the browser to open.
4. Upload raw files from the local dashboard.

The first run creates `.venv` and installs Python dependencies. Later runs reuse that environment.

## Data

Local analysis data is stored inside:

```text
data/
```

The latest dashboard files are stored inside:

```text
data/output/
```

## Stop

```powershell
powershell -ExecutionPolicy Bypass -File scripts\stop_server.ps1 -Port 8012
```

## Build a ZIP package

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package_local.ps1
```

The package is created at:

```text
dist/pro-local-runner.zip
```
