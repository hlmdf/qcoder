@echo off
rem Build a single-file exe. Output: dist\QRReader.exe
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto nodeps
".venv\Scripts\python.exe" -m pip install pyinstaller >nul 2>&1
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --noconsole --name QRReader main.py
echo.
if exist "dist\QRReader.exe" (
  echo Build finished: dist\QRReader.exe
) else (
  echo Build failed, scroll up for the error.
)
pause
exit /b 0
:nodeps
echo Run install.cmd first.
pause
exit /b 1
