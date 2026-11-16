@echo off
REM Double-click to start the retro board. Keep this window open during the retro.
cd /d "%~dp0"
where py >nul 2>nul && (py -3 retro.py %*) || (python retro.py %*)
pause
