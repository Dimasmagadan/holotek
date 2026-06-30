#!/bin/bash
cd "$(dirname "$0")"
nohup .venv/bin/python3 holotek.py --menubar --config config.json >/dev/null 2>&1 &
disown
echo "Holotek started (PID $!). Safe to close terminal."
echo "Quit from the menu bar icon dropdown."
