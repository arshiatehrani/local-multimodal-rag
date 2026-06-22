@echo off
REM ============================================================
REM  Multimodal RAG - one-click launcher
REM  Starts: Qdrant (Docker) -> Backend (uvicorn) -> Frontend
REM  Then waits for the backend and opens the browser.
REM  Double-click this file to run everything.
REM ============================================================
setlocal EnableExtensions

REM Always operate from the folder this script lives in.
cd /d "%~dp0"

REM Use the project venv's Python directly (no conda/venv activation needed,
REM all packages are installed inside venv\Lib\site-packages).
set "PY=%~dp0venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [ERROR] Could not find the venv Python at:
    echo         %PY%
    echo Create the venv first, or edit PY in this script.
    pause
    exit /b 1
)

echo.
echo [1/4] Starting Qdrant (Docker)...
docker compose up -d
if errorlevel 1 (
    echo.
    echo [ERROR] "docker compose up" failed.
    echo Make sure Docker Desktop is running, then try again.
    pause
    exit /b 1
)

echo.
echo [2/4] Launching backend  (http://127.0.0.1:8000) ...
REM --reload-dir limits the file watcher to backend/ ONLY. Without it, --reload
REM tries to watch the whole project (models/, venv/, .pip-cache/) which makes
REM the reloader choke on Windows. Bind explicitly to loopback IPv4.
start "RAG Backend" cmd /k ""%PY%" -m uvicorn main:app --app-dir backend --reload --reload-dir backend --host 127.0.0.1 --port 8000"

echo [3/4] Launching frontend (http://127.0.0.1:3000) ...
start "RAG Frontend" cmd /k ""%PY%" -m http.server 3000 --bind 127.0.0.1 --directory frontend"

echo.
echo [4/4] Waiting for the backend to come online...
set /a tries=0
:waitloop
set /a tries+=1
if %tries% gtr 60 (
    echo [WARN] Backend did not respond after ~2 min. Opening the page anyway.
    goto openbrowser
)
timeout /t 2 /nobreak >nul
curl -s -o nul http://127.0.0.1:8000/health
if errorlevel 1 goto waitloop

:openbrowser
echo.
echo Backend is up. Opening the app in your browser...
start "" http://127.0.0.1:3000/app.html

echo.
echo ============================================================
echo  All services started.
echo    Frontend : http://127.0.0.1:3000/app.html
echo    Backend  : http://127.0.0.1:8000  (logs in the "RAG Backend" window)
echo    Qdrant   : http://127.0.0.1:6333  (Docker)
echo.
echo  To STOP everything, run stop.bat (or close the two service
echo  windows; Qdrant keeps running in Docker until stop.bat).
echo ============================================================
echo.
echo This launcher window can be closed.
pause
endlocal
