@echo off
rem Run the built-in self-test (keep this file ASCII-only)
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto nopython

python "selftest.py" %*
echo.
pause
exit /b 0

:nopython
echo [!] Python not found in PATH. Please install Python 3.8 or newer first.
pause
exit /b 1
