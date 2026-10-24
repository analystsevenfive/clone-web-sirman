@echo off
rem Run the Sirman -> Google Sheet sync once, by hand. Add "dry" to test without writing:  run_sync.bat dry
cd /d "%~dp0"
if /i "%1"=="dry" set DRY_RUN=1
set PYTHONIOENCODING=utf-8
python sync_sirman_to_sheet.py
pause
