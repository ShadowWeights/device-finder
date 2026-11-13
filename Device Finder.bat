@echo off
title Device Finder - Keep this window open
cd /d "%~dp0"
".venv\Scripts\python.exe" device_finder.py
if errorlevel 1 pause
