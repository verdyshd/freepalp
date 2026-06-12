@echo off
chcp 65001 >nul 2>&1
title FreePalp AI Orchestrator
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found!
    echo         Download: https://python.org/downloads/
    echo         Check "Add Python to PATH" during install
    pause
    exit /b 1
)

if not exist .env (
    if exist .env.example (
        copy .env.example .env >nul
        echo  [INFO] .env created. Open Providers tab to add API keys.
    ) else (
        echo. > .env
    )
)

echo  [INFO] Checking dependencies...
pip install -r requirements.txt -q --disable-pip-version-check >nul 2>&1
if errorlevel 1 (
    echo  [INFO] Installing dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo  [ERROR] Dependency install failed
        pause
        exit /b 1
    )
)

for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":28800 " ^| findstr "LISTENING"') do (
    taskkill /PID %%p /F >nul 2>&1
)

start "" cmd /c "timeout /t 3 >nul && start http://localhost:28800"

set PYTHONUTF8=1
python -m freepalp.gateway

if errorlevel 1 (
    echo.
    echo  [ERROR] Server crashed. See error above.
    pause
)