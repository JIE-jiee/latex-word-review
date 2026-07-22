@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "BOOTSTRAP=%~dp0scripts\bootstrap-windows.ps1"
set "POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

if not exist "%BOOTSTRAP%" goto missing_bootstrap
if not exist "%POWERSHELL%" goto missing_powershell

"%POWERSHELL%" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%BOOTSTRAP%"
set "RESULT=%ERRORLEVEL%"
if "%RESULT%"=="0" exit /b 0

echo.
echo [ERROR] LaTeX Word Review could not start.
echo Read the message above, then press any key to close this window.
pause >nul
exit /b %RESULT%

:missing_bootstrap
echo [ERROR] scripts\bootstrap-windows.ps1 is missing.
echo Extract the complete GitHub source ZIP before starting the application.
pause
exit /b 2

:missing_powershell
echo [ERROR] Windows PowerShell 5.1 is unavailable.
pause
exit /b 3
