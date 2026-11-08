@echo off
rem Teste MocapCam_Biped.ms dans 3ds Max sans ouvrir l'interface (3dsmaxbatch) :
rem import de captures\prise_006\prise_006.fbx, Biped neuf, transfert, mesures dans test\resultat.txt
setlocal
set "MAXBATCH="
for /d %%D in ("%ProgramFiles%\Autodesk\3ds Max 20*") do if exist "%%D\3dsmaxbatch.exe" set "MAXBATCH=%%D\3dsmaxbatch.exe"
if not defined MAXBATCH (
  echo 3ds Max introuvable dans "%ProgramFiles%\Autodesk".
  pause
  exit /b 1
)
echo Test du script dans %MAXBATCH%
echo (1 a 5 minutes, 3ds Max tourne en arriere-plan)...
if exist "%~dp0test\resultat.txt" del "%~dp0test\resultat.txt"
"%MAXBATCH%" "%~dp0test\test_biped.ms" -listenerlog "%~dp0test\listener.log"
echo.
if exist "%~dp0test\resultat.txt" (type "%~dp0test\resultat.txt") else (echo Pas de resultat : voir test\listener.log)
pause
