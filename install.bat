@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist .venv (
    echo [1/3] Creating virtualenv...
    python -m venv .venv
    if errorlevel 1 goto fail
)

echo [2/3] Installing / updating packages...
.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
.venv\Scripts\python.exe -m pip install -U -r requirements.txt
if errorlevel 1 goto fail

echo [3/3] Checking bot token...
if not exist .env (
    copy /y .env.example .env >nul
    echo WARNING: created .env from .env.example - paste your BotFather token there.
) else (
    findstr /R /C:"^BOT_TOKEN=." .env >nul || echo WARNING: BOT_TOKEN is empty in .env!
)

echo.
echo Done. Edit .env (put your token), then run start.bat.
pause
exit /b 0

:fail
echo.
echo Install failed. Make sure Python 3.10+ is installed and in PATH.
pause
exit /b 1