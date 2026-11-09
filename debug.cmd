@echo off
rem 带控制台启动，出了错能看见
cd /d "%~dp0"
".venv\Scripts\python.exe" main.py %*
echo.
echo 退出码 %ERRORLEVEL%
pause
