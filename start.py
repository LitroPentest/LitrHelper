@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe bot.py
) else (
    python bot.py
)

echo.
echo Bot stopped.
pause