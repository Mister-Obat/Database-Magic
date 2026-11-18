@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%_bibliotheque_mtg") do set "OPS_ROOT=%%~fI"
set "PROG_DIR=%OPS_ROOT%\src\python"
set "PYTHON=%OPS_ROOT%\.venv\Scripts\python.exe"
set "MAGIC_PORT=%MAGIC_LIBRARY_PORT%"
if not defined MAGIC_PORT set "MAGIC_PORT=8765"
%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%Arreter_ancienne_Bibliotheque_Magic.ps1" -Port %MAGIC_PORT% -Python "%PYTHON%" -ProgDir "%PROG_DIR%"
if errorlevel 1 (
  echo.
  echo La Bibliotheque Magic n'a pas pu liberer son port. Consultez les messages ci-dessus.
  pause
  exit /b 1
)
endlocal
