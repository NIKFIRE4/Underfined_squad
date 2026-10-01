@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 server.py %*
  goto done
)
python server.py %*
:done
if errorlevel 1 (
  echo Server could not start. Check Python 3.10+ and whether port 8000 is free.
  pause
)
endlocal
