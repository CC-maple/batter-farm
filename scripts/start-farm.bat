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

if /I "%~1"=="--self-test" (
    "%YOLO_PYTHON%" --version
    exit /b %ERRORLEVEL%
)

"%YOLO_PYTHON%" -s main.py --overlay %*
exit /b %ERRORLEVEL%
