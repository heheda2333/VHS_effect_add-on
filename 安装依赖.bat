@echo off
rem Install dependencies (keep this file ASCII-only)
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto nopython

python "setup_deps.py"
if errorlevel 1 goto failed
echo.
pause
exit /b 0

:nopython
echo [!] Python not found in PATH. Please install Python 3.8 or newer first.
pause
exit /b 1

:failed
echo.
echo [!] Dependency installation failed. See the message above.
pause
exit /b 1
