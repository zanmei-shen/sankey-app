@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found.
    echo Create it first with: python -m venv .venv
    pause
    exit /b 1
)

echo Starting Sankey app at http://127.0.0.1:8000
echo Press Ctrl+C to stop the server.
".venv\Scripts\python.exe" -m uvicorn app:app --reload

pause
