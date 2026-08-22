@echo off
rem Builds a shareable Windows package. Double click it, or: build.bat nozip
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo python was not found on PATH
    pause
    exit /b 1
)

python build.py %*
if errorlevel 1 (
    echo.
    echo BUILD FAILED
    pause
    exit /b 1
)

echo.
pause
