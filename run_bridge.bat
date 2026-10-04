@echo off
title Blender MCP Cloud Bridge (ngrok)
cd /d "%~dp0"
echo ========================================================
echo Starting Blender MCP Cloud Bridge with ngrok...
echo ========================================================
python bridge.py --tunnel ngrok
if errorlevel 1 (
    echo.
    echo Bridge exited with an error.
    pause
)
