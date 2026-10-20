@echo off
chcp 65001 >nul
setlocal EnableExtensions
title QQ Bot - Stop All

echo ============================================================
echo    Stopping all QQ Bot components...
echo ============================================================
echo.

echo [1/4] Bot server (port 3001)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":3001" ^| findstr "LISTENING"') do (
    taskkill /f /pid %%a >nul 2>&1
)
echo       done

echo [2/4] NapCat / QQ
taskkill /f /im QQ.exe >nul 2>&1
echo       done

echo [3/4] WorkBuddy gateway (port 8788)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8788" ^| findstr "LISTENING"') do (
    taskkill /f /pid %%a >nul 2>&1
)
echo       done

echo [4/4] Control panel (port 8789)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8789" ^| findstr "LISTENING"') do (
    taskkill /f /pid %%a >nul 2>&1
)
echo       done

echo.
echo ============================================================
echo    All stopped.
echo ============================================================
echo.
pause
