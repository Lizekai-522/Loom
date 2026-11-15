@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_verilo.ps1" -Mode desktop
if errorlevel 1 pause
