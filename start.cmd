@echo off
rem 正常启动：不弹控制台窗口
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" goto novenv
start "" ".venv\Scripts\pythonw.exe" main.py
exit /b 0
:novenv
echo 还没装依赖，先运行 install.cmd
pause
exit /b 1
