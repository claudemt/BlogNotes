@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title BlogNotes
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0src\launch.ps1"
exit /b %errorlevel%
