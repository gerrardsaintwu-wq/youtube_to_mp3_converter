@echo off
setlocal
title YouToMedia Server

REM Run from the folder this .bat lives in, regardless of where it was launched from
cd /d "%~dp0"

REM --- Sanity checks ---
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found in PATH. Install Python 3.8+ first.
    pause
    exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo [ERROR] FFmpeg was not found in PATH. Run: winget install Gyan.FFmpeg
    pause
    exit /b 1
)

python -c "import waitress" >nul 2>nul
if errorlevel 1 (
    echo [SETUP] Installing Python dependencies ^(first run only^)...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency install failed. Check your internet connection.
        pause
        exit /b 1
    )
)

echo.
echo ================================================
echo   YouToMedia is starting...
echo   Local:    http://localhost:5000
echo   Public:   https://finale-thesis-accompany.ngrok-free.dev
echo   Close the Server window to stop, or Ctrl+C here
echo ================================================
echo.

REM Start the Flask/Waitress server in its own window
start "YouToMedia Server" cmd /k "python serve.py"

REM Give the server a moment to boot before tunneling traffic
timeout /t 3 /nobreak >nul

echo [*] Starting ngrok tunnel...
ngrok http --url=finale-thesis-accompany.ngrok-free.dev 5000
echo.
echo [!] ngrok exited. Closing server...
taskkill /fi "WINDOWTITLE eq YouToMedia Server*" >nul 2>nul
pause
