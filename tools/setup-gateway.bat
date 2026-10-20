@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
title WorkBuddy Gateway Setup

set "ROOT=%~dp0.."
cd /d "%ROOT%"

set "VER=1.7.16"
set "URL=https://github.com/ardeyouxipianyi/workbuddy2api-hub/releases/download/v%VER%/workbuddy2api-hub-v%VER%.zip"
set "ZIP=%TEMP%\wb-gateway.zip"
set "TMPX=%TEMP%\wb-gw-ext"
set "DEST=%USERPROFILE%\workbuddy-gateway"

echo ============================================================
echo    WorkBuddy Gateway Auto Setup
echo ============================================================
echo.
echo    This gateway wraps your WorkBuddy account into an
echo    OpenAI-compatible API, so the bot can use it.
echo.
echo    If you use your own API key (DeepSeek etc.), you can
echo    SKIP this step.
echo.

choice /c YN /m "    Install WorkBuddy gateway now"
if errorlevel 2 goto :skip

REM ---- existing ----
if exist "%DEST%\start-wb-proxy.bat" (
    echo.
    echo    Already installed: %DEST%
    echo    Opening panel...
    start "" "http://127.0.0.1:8788/"
    goto :done
)

REM ---- download ----
echo.
echo [1/3] Downloading v%VER% ...
if exist "%ZIP%" del /q "%ZIP%" >nul 2>&1
set "OK=0"
where curl.exe >nul 2>&1
if not errorlevel 1 (
    curl.exe -L --fail --silent --show-error -o "%ZIP%" "%URL%"
    if not errorlevel 1 set "OK=1"
)
if "%OK%"=="0" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue';try{Invoke-WebRequest -Uri '%URL%' -OutFile '%ZIP%' -UseBasicParsing;exit 0}catch{exit 1}"
    if not errorlevel 1 set "OK=1"
)
if "%OK%"=="0" (
    echo.
    echo    [ERROR] Download failed.
    echo    Download manually:
    echo      %URL%
    echo    Save to: %ZIP%  then re-run this script.
    pause
    exit /b 1
)

REM ---- extract ----
echo [2/3] Extracting...
if exist "%TMPX%" rmdir /s /q "%TMPX%" >nul 2>&1
mkdir "%TMPX%" >nul 2>&1
where tar.exe >nul 2>&1
if not errorlevel 1 (
    tar.exe -xf "%ZIP%" -C "%TMPX%" >nul 2>&1
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%ZIP%' -DestinationPath '%TMPX%' -Force"
)

set "SRC="
for /d %%D in ("%TMPX%\*") do (
    if exist "%%D\start-wb-proxy.bat" set "SRC=%%D"
)
if "%SRC%"=="" if exist "%TMPX%\start-wb-proxy.bat" set "SRC=%TMPX%"
if "%SRC%"=="" (
    echo    [ERROR] start-wb-proxy.bat not found in archive.
    pause
    exit /b 1
)

if not exist "%DEST%" mkdir "%DEST%" >nul 2>&1
robocopy "%SRC%" "%DEST%" /E /NFL /NDL /NJH /NJS /NC /NS /NP >nul
echo       Installed to: %DEST%

REM ---- deps + launch ----
echo [3/3] Starting gateway...
python -c "import flask" >nul 2>&1
if errorlevel 1 (
    if exist "%DEST%\requirements.txt" python -m pip install -r "%DEST%\requirements.txt" >nul 2>&1
)
start "WorkBuddy Gateway" /MIN /D "%DEST%" cmd /c start-wb-proxy.bat
timeout /t 6 /nobreak >nul

echo.
echo ============================================================
echo    Gateway started
echo.
echo    Panel  : http://127.0.0.1:8788/
echo    Password: admin
echo    API    : http://127.0.0.1:8788/v1
echo.
echo    In the panel:
echo      1. Click "+ Add Account (OAuth)", log in
echo      2. Settings page: generate an API Key
echo      3. Back to QQ Bot console: paste the Key
echo ============================================================
echo.
start "" "http://127.0.0.1:8788/"
goto :done

:skip
echo.
echo    Skipped. You can use any OpenAI-compatible API instead.

:done
echo.
pause
exit /b 0
