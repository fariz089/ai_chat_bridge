@echo off
title FakeFluencer V2 - Web
echo ========================================
echo    FakeFluencer V2 - Web UI
echo ========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found! Install Python 3.10+ first.
    pause
    exit /b 1
)

if not exist ".venv" (
    echo [*] Creating virtual environment...
    python -m venv .venv
    call .venv\Scripts\activate
    echo [*] Installing dependencies...
    pip install -r requirements.txt
    pip install -r webapp\requirements-web.txt
    echo [*] Installing Playwright browser...
    playwright install chromium
) else (
    call .venv\Scripts\activate
)

echo.
echo [*] Starting web UI on http://127.0.0.1:5000/dashboard
echo.
python webapp\app.py --port 5000
pause
