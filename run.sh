#!/bin/bash
echo "========================================"
echo "   AI Chat Bridge - Launcher"
echo "========================================"
echo

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "[ERROR] Python3 not found! Install Python 3.10+ first."
    exit 1
fi

# Create venv if needed
if [ ! -d ".venv" ]; then
    echo "[*] Creating virtual environment..."
    python3 -m venv .venv
    source .venv/bin/activate
    echo "[*] Installing dependencies..."
    pip install -r requirements.txt
    echo "[*] Installing Playwright browsers..."
    playwright install chromium
else
    source .venv/bin/activate
fi

echo
echo "[*] Starting AI Chat Bridge..."
echo
python3 ai_chat_bridge.py
