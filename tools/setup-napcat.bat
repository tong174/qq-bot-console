@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
title NapCat Setup

set "ROOT=%~dp0.."
cd /d "%ROOT%"

set "VER=4.18.38"
set "URL=https://github.com/NapNeko/NapCatQQ/releases/download/v%VER%/NapCat.Shell.Windows.OneKey.zip"
set "ZIP=%TEMP%\NapCat.OneKey.zip"
set "DEST=%ROOT%\NapCat.Framework"

echo ============================================================
echo    NapCat Auto Setup
echo ============================================================
echo.

REM ---- 0. QQ check ----
echo [0/4] Checking QQ installation...
for /f "tokens=2*" %%a in ('reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ" /v UninstallString 2^>nul') do (
    for %%p in (%%~b) do set "QQDIR=%%~dp"
)
if not defined QQQDIR set "QQDIR="
if "%QQDIR%"=="" (
    reg query "HKLM\SOFTWARE\Oracle\JavaSoft" >nul 2>&1
)
if "%QQDIR%"=="" (
    echo       [WARN] QQ not found in registry.
    echo       NapCat needs the official QQ client ^(NTQQ^) to be installed.
    echo       Download: https://im.qq.com/
    echo.
    pause
    exit /b 1
)
echo       QQ found: %QQDIR%

REM ---- 1. already installed? ----
echo [1/4] Checking existing NapCat...
if exist "%DEST%\napiloader.dll" (
    echo       NapCat already present at:
    echo       %DEST%
    echo.
    choice /c YN /m "      Reinstall"
    if errorlevel 2 goto :done
)

REM ---- 2. download ----
echo [2/4] Downloading NapCat v%VER% ...
if exist "%ZIP%" del /q "%ZIP%" >nul 2>&1

set "OK=0"
where curl.exe >nul 2>&1
if not errorlevel 1 (
    curl.exe -L --fail --silent --show-error -o "%ZIP%" "%URL%"
    if not errorlevel 1 set "OK=1"
)
if "%OK%"=="0" (
    echo       curl failed, trying PowerShell...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue';try{Invoke-WebRequest -Uri '%URL%' -OutFile '%ZIP%' -UseBasicParsing;exit 0}catch{exit 1}"
    if not errorlevel 1 set "OK=1"
)
if "%OK%"=="0" (
    echo.
    echo       [ERROR] Download failed.
    echo       Download manually in your browser:
    echo         %URL%
    echo       Save to: %ZIP%
    echo       Then run this script again.
    pause
    exit /b 1
)
for %%I in ("%ZIP%") do echo       Downloaded: %%~zI bytes

REM ---- 3. extract ----
echo [3/4] Extracting...
set "TMPX=%TEMP%\napcat_ext_%RANDOM%"
mkdir "%TMPX%" >nul 2>&1
where tar.exe >nul 2>&1
if not errorlevel 1 (
    tar.exe -xf "%ZIP%" -C "%TMPX%" >nul 2>&1
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%ZIP%' -DestinationPath '%TMPX%' -Force"
)
if errorlevel 1 (
    echo       [ERROR] Extract failed.
    pause
    exit /b 1
)

if not exist "%DEST%" mkdir "%DEST%" >nul 2>&1
robocopy "%TMPX%" "%DEST%" /E /NFL /NDL /NJH /NJS /NC /NS /NP >nul
rmdir /s /q "%TMPX%" >nul 2>&1
echo       Installed to: %DEST%

REM ---- 4. write OneBot config ----
echo [4/4] Writing OneBot configuration...
python "%ROOT%\tools\napcat_config.py" "%DEST%" "%ROOT%"
if errorlevel 1 (
    echo       [WARN] Auto config failed, you can configure via NapCat WebUI.
)

:done
echo.
echo ============================================================
echo    NapCat setup finished
echo.
echo    Next steps:
echo      1. Start the bot from the control panel
echo         ^(run start.bat, click "Start All"^)
echo      2. NapCat will launch QQ, scan the QR code to log in
echo      3. Send a message to your bot in QQ
echo ============================================================
echo.
pause
exit /b 0
