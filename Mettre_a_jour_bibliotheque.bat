@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%") do set "PROJECT_ROOT=%%~fI"
for %%I in ("%PROJECT_ROOT%\_bibliotheque_mtg") do set "OPS_ROOT=%%~fI"
set "PYTHON=%OPS_ROOT%\.venv\Scripts\python.exe"
set "PROG_DIR=%OPS_ROOT%\src\python"
if defined MAGIC_LIBRARY_CONFIG (
  for %%I in ("%MAGIC_LIBRARY_CONFIG%") do set "CONFIG=%%~fI"
) else (
  set "CONFIG=%OPS_ROOT%\config.json"
)
if not exist "%PYTHON%" (
  >&2 echo Python de l'environnement isole introuvable : "%PYTHON%"
  endlocal & exit /b 4
)
if not exist "%CONFIG%" (
  >&2 echo Configuration introuvable : "%CONFIG%"
  endlocal & exit /b 2
)
if not exist "%PROG_DIR%\bibliotheque_mtg\__main__.py" (
  >&2 echo Programme de bibliotheque Magic introuvable : "%PROG_DIR%"
  endlocal & exit /b 4
)
pushd "%PROG_DIR%" >nul
if errorlevel 1 (
  >&2 echo Impossible d'acceder au programme : "%PROG_DIR%"
  endlocal & exit /b 4
)
set "PYTHONIOENCODING=utf-8"
"%PYTHON%" -m bibliotheque_mtg --config "%CONFIG%" update %*
set "RC=%ERRORLEVEL%"
popd
echo Code de sortie : %RC%
endlocal & exit /b %RC%
