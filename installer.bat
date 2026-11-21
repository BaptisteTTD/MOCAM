@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Installation de MOCAM ===
where py >nul 2>nul
if errorlevel 1 (
  echo Python est introuvable. Installez Python 3.11 ou 3.12 depuis https://www.python.org
  echo en cochant "Add python.exe to PATH", puis relancez ce fichier.
  pause
  exit /b 1
)
if not exist .venv (
  py -3 -m venv .venv
)
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if not exist studio\studio.json (
  .venv\Scripts\python.exe -m mocapcam init studio
)
echo.
echo Installation terminee.
echo  - Mesurez vos boules et editez studio\studio.json
pause
