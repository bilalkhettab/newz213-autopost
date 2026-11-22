#!/bin/zsh
# Double-click to open the Newz 213 desk in your browser.
cd "$(dirname "$0")"
( sleep 2; open "http://localhost:8213" ) &
exec .venv/bin/python dashboard.py
