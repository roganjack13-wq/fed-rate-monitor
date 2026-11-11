@echo off
chcp 65001 >nul
setlocal
rem Fed rate monitor - Windows installer. Finds Python 3.9+, then runs install.py in the parent folder.
cd /d "%~dp0.."
set "PY="
py -3 -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo Python 3.9 or newer was not found. Install it from python.org and tick "Add python.exe to PATH".
  start "" https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
%PY% -X utf8 install.py %*
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
