@echo off
cd /d "%~dp0"
if not exist ".venv" python -m venv .venv
".venv\Scripts\python.exe" -m pip install -r requirements.txt
echo.
echo 装好了，双击 start.cmd 启动
pause
