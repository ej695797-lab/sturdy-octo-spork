@echo off
rem Start the Yeeps 1.31.0-blaze server on this PC (Windows).
rem   start.bat            run the server (keep this window open)
rem   start.bat --tunnel   also open a free Cloudflare quick tunnel in a second window
rem First run: creates server\.venv, installs requirements, creates server\.env.
setlocal
cd /d "%~dp0server"
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul || (echo Install Python 3.11 or newer from https://www.python.org/downloads/ and tick "Add python.exe to PATH". The "python" that opens the Microsoft Store does not work. & pause & exit /b 1)
if not exist .venv (
  echo Setting up Python packages ^(first run^)...
  python -m venv .venv || (pause & exit /b 1)
  .venv\Scripts\python -m pip install --upgrade pip >nul
  .venv\Scripts\python -m pip install -r requirements.txt || (pause & exit /b 1)
)
.venv\Scripts\python setup_env.py
set PORT=8000
for /f "tokens=1,* delims==" %%a in ('findstr /b "PORT=" .env') do set PORT=%%b

if /i "%~1"=="--tunnel" (
  where cloudflared >nul 2>nul || (echo cloudflared isn't installed: winget install --id Cloudflare.cloudflared & pause & exit /b 1)
  start "Yeeps tunnel - the https address is in this window" cloudflared tunnel --url http://127.0.0.1:%PORT%
  echo The tunnel opened in a second window: look for https://....trycloudflare.com there.
  echo Quick-tunnel addresses change every time; see README for a fixed address.
)

echo Server on port %PORT% (close this window to stop). Editor: http://127.0.0.1:%PORT%/editor
.venv\Scripts\python -m uvicorn main:app --host 0.0.0.0 --port %PORT%
pause
