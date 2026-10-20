@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

title QQ Robot

echo ============================================================
echo    QQ Robot - Launcher
echo ============================================================
echo.

REM ---- Find Python ----
set "PY="
for %%P in (python.exe python3.exe) do (
    if not defined PY (
        where %%P >nul 2>&1 && set "PY=%%P"
    )
)
if not defined PY (
    if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    if exist "%ProgramFiles%\Python311\python.exe" set "PY=%ProgramFiles%\Python311\python.exe"
)
if not defined PY (
    echo [ERROR] Python not found.
    echo.
    echo   Please install Python 3.9 or newer:
    echo   https://www.python.org/downloads/
    echo   Remember to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

echo [OK] Python: %PY%

REM ---- Check dependencies ----
"%PY%" -c "import websockets, yaml" >nul 2>&1
if errorlevel 1 (
    echo [..] Installing dependencies...
    "%PY%" -m pip install -q "websockets>=12.0" "PyYAML>=6.0" "Pillow>=9.0"
    if errorlevel 1 (
        echo [ERROR] Install failed. Run manually:
        echo         "%PY%" -m pip install websockets PyYAML Pillow
        pause
        exit /b 1
    )
)
echo [OK] Dependencies ready

REM ---- Check port ----
"%PY%" -c "import socket,sys;s=socket.socket();r=s.connect_ex(('127.0.0.1',8789));s.close();sys.exit(0 if r==0 else 1)" >nul 2>&1
if not errorlevel 1 (
    echo.
    echo [!] Console already running. Open browser...
    start "" "http://127.0.0.1:8789"
    timeout /t 2 >nul
    exit /b 0
)

echo.
echo ============================================================
echo    Starting...
echo    Browser will open in a few seconds.
echo    Keep this window open (or minimize it).
echo ============================================================
echo.

REM ---- Start backend (opens browser itself) ----
"%PY%" console.py --port 8789

echo.
echo Console stopped.
pause
