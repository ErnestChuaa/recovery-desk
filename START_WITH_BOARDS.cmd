@echo off
rem Starts Recovery Desk so the ESP32 boards on the same hotspot can reach it.
rem Keep this window open. The board address is printed below and shown on the dashboard.
cd /d "%~dp0"
if exist "%~dp0flights.csv" set FLIGHTS_CSV=%~dp0flights.csv
set PY=
if exist "%~dp0.venv\Scripts\python.exe" set PY="%~dp0.venv\Scripts\python.exe"
if not defined PY (py -3.12 -c "import fastapi, ortools" >nul 2>&1 && set PY=py -3.12)
if not defined PY (echo Could not find Python 3.12 with the requirements installed. Run: py -3.12 -m pip install -r requirements.txt & pause & exit /b 1)
echo Dashboard: http://localhost:8000/ui
echo If Windows asks, allow Python on Private AND Public networks (phone hotspots are often "Public").
%PY% -m uvicorn recovery.api:app --host 0.0.0.0 --port 8000 --workers 1
pause
