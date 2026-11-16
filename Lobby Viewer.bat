@echo off
cd /d "%~dp0"
echo Installing what Lobby Viewer needs...
pip install -q psutil requests pywebview
start "" pythonw lobby_viewer.pyw
