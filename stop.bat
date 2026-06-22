@echo off
REM ============================================================
REM  Multimodal RAG - stop everything started by run.bat
REM  Stops the backend (port 8000), frontend (port 3000), and
REM  the Qdrant Docker container.
REM ============================================================
setlocal EnableExtensions
cd /d "%~dp0"

echo Stopping backend (port 8000)...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1

echo Stopping frontend (port 3000)...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":3000" ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1

REM Close the leftover service console windows if they're still open.
taskkill /FI "WINDOWTITLE eq RAG Backend*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq RAG Frontend*" /T /F >nul 2>&1

echo Stopping Qdrant (Docker)...
docker compose stop >nul 2>&1

echo.
echo All services stopped.
echo (Qdrant data is preserved; "docker compose down -v" would delete it.)
pause
endlocal
