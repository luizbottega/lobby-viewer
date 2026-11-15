@echo off
cd /d "%~dp0"
pip install -q psutil requests
start "" pythonw lobby_viewer.pyw
