@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo RF LIQUIDITY PRO - WINDOWS STARTUP
echo ============================================================

if not exist ".venv\Scripts\python.exe" (
  echo [SETUP] Creating virtual environment...
  py -3 -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Python 3 was not found or venv creation failed.
    pause
    exit /b 1
  )
)

call ".venv\Scripts\activate.bat"

echo [SETUP] Checking runtime dependencies...
python -c "import ccxt,pandas,numpy,flask,requests,dotenv" >nul 2>&1
if errorlevel 1 (
  echo [SETUP] Missing dependency detected. Installing requirements...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    echo Check internet access and requirements.txt.
    pause
    exit /b 1
  )
) else (
  echo [SETUP] Dependencies already installed.
)

if not exist ".env" (
  copy /Y ".env.example" ".env" >nul
  echo [SETUP] Created .env from .env.example.
  echo Add your API credentials before enabling LIVE mode.
)

echo [CHECK] Verifying Python source tree...
python verify_project.py
if errorlevel 1 (
  echo [ERROR] Source verification failed. Bot was NOT started.
  pause
  exit /b 1
)

echo [CHECK] Running isolated regression tests...
python tools\run_isolated_tests.py
if errorlevel 1 (
  echo [ERROR] Structural tests failed. Bot was NOT started.
  pause
  exit /b 1
)

echo.
echo [START] RF Liquidity Pro
echo [START] Dashboard: http://127.0.0.1:8000 (or PORT from .env)
echo.
python main.py

echo.
echo [STOP] RF Liquidity Pro exited.
pause
