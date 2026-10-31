@echo off
setlocal
cd /d "%~dp0"

rem Environnement Python dans un chemin court : Windows refuse de charger des DLL au-dela de 260 caracteres.
set "VENV=%LOCALAPPDATA%\LecteurSecret\venv"
set "PY=%VENV%\Scripts\python.exe"

if not exist "%PY%" (
  echo Premiere installation, 1 a 2 minutes...
  python -m venv "%VENV%" || goto :erreur
  "%PY%" -m pip install --quiet --upgrade pip
  "%PY%" -m pip install --quiet -r requirements.txt || goto :erreur
)

"%PY%" lecteur_secret.py %*
if errorlevel 1 pause
exit /b

:erreur
rem installation ratee : on efface l'environnement pour tout reprendre au prochain lancement
if exist "%VENV%" rmdir /s /q "%VENV%"
echo.
echo Installation impossible. Verifie que Python est installe : https://www.python.org
pause
