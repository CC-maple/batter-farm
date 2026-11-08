@echo off
setlocal EnableExtensions
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "YOLO_PYTHON=%ROOT%\.venv\Scripts\python.exe"
cd /d "%ROOT%"
if not exist "%YOLO_PYTHON%" (
    echo Create the project .venv and install requirements.txt first.
    exit /b 1
)
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONNOUSERSITE=1"

set "PWSH_EXE=C:\Program Files\PowerShell\7\pwsh.exe"
if /I "%~1"=="--demo" goto run
if /I "%~1"=="--help" goto run
if not exist "%PWSH_EXE%" (
    echo PowerShell 7 is required for elevation.
    exit /b 1
)
"%PWSH_EXE%" -NoLogo -NoProfile -Command "$p=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()); if ($p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 0 } else { exit 1 }"
if not errorlevel 1 goto run
"%PWSH_EXE%" -NoLogo -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
exit /b %ERRORLEVEL%
:run
"%YOLO_PYTHON%" -s -m src.ui.app %*
exit /b %ERRORLEVEL%
