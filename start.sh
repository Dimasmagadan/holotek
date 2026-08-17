#!/bin/bash
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python3 ]; then
    echo "holotek: .venv not found. Run: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt" >&2
    exit 1
fi
nohup .venv/bin/python3 holotek.py --menubar --config config.json >>/tmp/holotek_app.log 2>&1 &
disown
echo "Holotek started (PID $!). Safe to close terminal."
echo "Quit from the menu bar icon dropdown."
