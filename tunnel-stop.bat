@echo off
chcp 65001 >nul 2>&1
title Stop FreePalp Tunnel
echo.
echo  [INFO] Stopping all cloudflared tunnels...
taskkill /IM cloudflared.exe /F >nul 2>&1
if errorlevel 1 (
    echo  [INFO] No running tunnels found.
) else (
    echo  [OK] Tunnel stopped.
)
echo.
timeout /t 2 >nul