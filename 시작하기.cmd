@echo off
chcp 65001 >nul
set "PROJECT_ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_ROOT%scripts\Start-Menu.ps1"
if errorlevel 1 pause

