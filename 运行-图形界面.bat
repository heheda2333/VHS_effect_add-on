@echo off
rem GUI launcher (keep this file ASCII-only)
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto nopython

python "check_env.py" --quiet
if errorlevel 1 goto nodeps

python "gui.py"
if errorlevel 1 goto failed
exit /b 0

:nopython
echo [!] Python not found in PATH. Please install Python 3.8 or newer first.
pause
exit /b 1

:nodeps
echo.
echo [!] Missing dependencies. Please run the install script first.
pause
exit /b 1

:failed
echo.
echo [!] The GUI exited with an error. See the message above.
pause
exit /b 1
