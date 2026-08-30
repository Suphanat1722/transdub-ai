@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set TORCH_HOME=%CD%\models\demucs
if not exist ".venv\Scripts\python.exe" (
  echo TransDub AI is not installed. Running setup...
  powershell -ExecutionPolicy Bypass -File ".\Setup.ps1"
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
if not exist ".env" copy ".env.example" ".env" >nul
.venv\Scripts\python.exe run.py
if errorlevel 1 pause
