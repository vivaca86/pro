@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap_local.ps1" %*
set EXIT_CODE=%ERRORLEVEL%

if not "%EXIT_CODE%"=="0" (
  echo.
  echo Local dashboard failed to start.
  echo Check the message above, then run start-local.bat again.
  echo.
  pause
  exit /b %EXIT_CODE%
)

echo.
echo Local dashboard started. This window will close soon.
timeout /t 5 >nul
exit /b 0
