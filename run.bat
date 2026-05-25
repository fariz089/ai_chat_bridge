@echo off
title AI Chat Bridge
echo ========================================
echo    AI Chat Bridge - Launcher
echo ========================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found! Install Python 3.10+ first.
    pause
    exit /b 1
)

:: Install dependencies if needed
if not exist ".venv" (
    echo [*] Creating virtual environment...
    python -m venv .venv
    call .venv\Scripts\activate
    echo [*] Installing dependencies...
    pip install -r requirements.txt
    echo [*] Installing Playwright browsers...
    playwright install chromium
) else (
    call .venv\Scripts\activate
)

echo.
echo [*] Starting AI Chat Bridge...
echo.
python ai_chat_bridge.py

pause
