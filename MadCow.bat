@echo off
rem ===========================================================================
rem  MadCow Charts - lanzador para Windows
rem  Doble clic: arranca la app y abre el navegador. Si ya estaba en marcha, solo
rem  abre el navegador. La primera vez instala lo necesario (ib_async).
rem  Admite las mismas opciones que server.py, por ejemplo:
rem      MadCow.bat --lan --password clave
rem  Para cerrar la app, cierra esta ventana (o pulsa Ctrl+C).
rem ===========================================================================
setlocal
cd /d "%~dp0"
title MadCow Charts

rem Python: el lanzador "py" si esta instalado; si no, "python"
set "PY=python"
where py >nul 2>nul && set "PY=py"
%PY% --version >nul 2>nul || goto :nopython

rem Dependencias: solo la primera vez
%PY% -c "import ib_async" >nul 2>nul || (
  echo Instalando lo necesario, solo la primera vez...
  %PY% -m pip install -r requirements.txt || goto :error
)

%PY% server.py --open %*
if errorlevel 1 goto :error
exit /b 0

:nopython
echo No se ha encontrado Python. Instalalo desde https://www.python.org/downloads/
echo y vuelve a abrir MadCow.
pause
exit /b 1

:error
echo.
echo MadCow se ha detenido por un error: revisa el mensaje de arriba.
pause
exit /b 1
