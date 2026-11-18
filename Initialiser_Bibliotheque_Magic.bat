@echo off
setlocal
set "OPS_ROOT=%~dp0_bibliotheque_mtg"
set "PYTHON=%OPS_ROOT%\.venv\Scripts\python.exe"
set "PROG_DIR=%OPS_ROOT%\src\python"
if defined MAGIC_LIBRARY_CONFIG (
  for %%I in ("%MAGIC_LIBRARY_CONFIG%") do set "CONFIG=%%~fI"
) else (
  set "CONFIG=%OPS_ROOT%\config.json"
)
if not exist "%PYTHON%" (
  >&2 echo Environnement Python absent. Suivez les etapes d'installation du README.
  exit /b 4
)
if not exist "%CONFIG%" (
  >&2 echo Configuration absente. Copiez config.example.json vers config.json comme indique dans le README.
  exit /b 2
)
pushd "%PROG_DIR%" >nul || exit /b 4
set "PYTHONIOENCODING=utf-8"
"%PYTHON%" -m bibliotheque_mtg --config "%CONFIG%" init %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
