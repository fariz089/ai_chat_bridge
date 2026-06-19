#!/bin/bash
echo "========================================"
echo "   FakeFluencer V2 - Web UI"
echo "========================================"
echo

if ! command -v python3 &> /dev/null; then
    echo "[ERROR] Python3 not found! Install Python 3.10+ first."
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "[*] Creating virtual environment..."
    python3 -m venv .venv
    source .venv/bin/activate
    echo "[*] Installing dependencies..."
    pip install -r requirements.txt
    pip install -r webapp/requirements-web.txt
    echo "[*] Installing Playwright browser..."
    playwright install chromium
else
    source .venv/bin/activate
fi

echo
echo "[*] Starting web UI on http://127.0.0.1:5000/dashboard"
echo
# On a headless server add: --headless true
python3 webapp/app.py --port 5000
