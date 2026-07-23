@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion

set "BOOTSTRAP=%~dp0scripts\bootstrap-windows.ps1"
set "POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

if not exist "%BOOTSTRAP%" goto missing_files
if not exist "%POWERSHELL%" goto missing_powershell

"%POWERSHELL%" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%BOOTSTRAP%"
set "RESULT=%ERRORLEVEL%"
if "%RESULT%"=="0" exit /b 0

echo.
echo [E_LAUNCHER_BOOTSTRAP_FAILED] 启动未完成，请查看上方错误码后重试。
echo [E_LAUNCHER_BOOTSTRAP_FAILED] Startup did not finish. Read the error code above and retry.
echo 按任意键关闭此窗口。 / Press any key to close this window.
pause >nul
exit /b %RESULT%

:missing_files
echo.
echo [E_LAUNCHER_FILES_MISSING] 项目文件不完整，请重新“全部解压”整个源码 ZIP。
echo [E_LAUNCHER_FILES_MISSING] Project files are incomplete. Extract the complete source ZIP again.
echo 按任意键关闭此窗口。 / Press any key to close this window.
pause >nul
exit /b 2

:missing_powershell
echo.
echo [E_LAUNCHER_POWERSHELL_MISSING] 未找到 Windows PowerShell 5.1。
echo [E_LAUNCHER_POWERSHELL_MISSING] Windows PowerShell 5.1 is unavailable.
echo 按任意键关闭此窗口。 / Press any key to close this window.
pause >nul
exit /b 3
