@echo off
chcp 65001 >nul 2>&1
title FreePalp Tunnel
cd /d "%~dp0"

set "CF=C:\Program Files (x86)\cloudflared\cloudflared.exe"
if not exist "%CF%" (
    echo [ERROR] cloudflared not found.
    echo Install it: winget install Cloudflare.cloudflared
    pause
    exit /b 1
)

echo.
echo  +------------------------------------------+
echo  ^|       FreePalp Cloudflare Tunnel         ^|
echo  +------------------------------------------+
echo.
echo  [INFO] FreePalp must be running first (start.bat).
echo  [INFO] Closing any old tunnels...
taskkill /IM cloudflared.exe /F >nul 2>&1

echo.
echo  [INFO] Starting tunnel to http://localhost:28800
echo  [INFO] Your PUBLIC URL appears below (https://...trycloudflare.com)
echo  [INFO] Copy it and send to your friend.
echo  [INFO] Keep this window open. Close it to STOP the tunnel.
echo.

"%CF%" tunnel --url http://localhost:28800