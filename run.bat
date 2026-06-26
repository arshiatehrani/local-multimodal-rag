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

echo Waiting for Qdrant to be ready (can take ~30s after a restart)...
set /a qtries=0
:qdrantwait
set /a qtries+=1
if %qtries% gtr 45 (
    echo [WARN] Qdrant did not respond on port 6333. Backend may fail to start.
    goto backendstart
)
timeout /t 2 /nobreak >nul
curl -s -o nul http://127.0.0.1:6333/
if errorlevel 1 goto qdrantwait
echo Qdrant is up.

:backendstart
echo.
REM Configure model warmup on startup:
REM "all" (default): Loads all 3 models sequentially. Takes ~2-3 mins but everything is instant later.
REM "embedder": Only loads the embedder. Faster startup.
REM "none": Starts instantly. Models spin up on-demand during your first chat/upload.
set "PRELOAD_MODELS=all"

echo [2/4] Launching backend  (http://127.0.0.1:8000) ...
REM NO --reload here on purpose: this is a "use the app" launcher, not a dev
REM server. On Windows --reload makes WatchFiles try to watch the whole project
REM (venv/, models/, .pip-cache/), which pegs the process and makes it
REM unresponsive ("offline" even though it bound the port). Bind to loopback IPv4.
start "RAG Backend" cmd /k ""%PY%" -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000"

echo [3/4] Launching frontend (http://127.0.0.1:3000) ...
start "RAG Frontend" cmd /k ""%PY%" -m http.server 3000 --bind 127.0.0.1 --directory frontend"

echo.
echo [4/4] Opening the app in your browser...
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
