@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_verilo.ps1" -Mode web
if errorlevel 1 pause
