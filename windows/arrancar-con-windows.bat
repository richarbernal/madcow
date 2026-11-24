@echo off
rem ===========================================================================
rem  Hace que MadCow arranque solo al iniciar sesion en Windows: crea un acceso
rem  directo en la carpeta Inicio que abre MadCow.bat minimizado y sin navegador.
rem  Las opciones que anadas se pasan a la app, por ejemplo:
rem      arrancar-con-windows.bat --lan --password clave
rem  Para deshacerlo: quitar-de-inicio.bat
rem ===========================================================================
setlocal
for %%I in ("%~dp0..") do set "MADCOW_DIR=%%~fI"
set "MADCOW_ARGS=--no-open %*"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup') + '\MadCow.lnk');" ^
  "$s.TargetPath = Join-Path $env:MADCOW_DIR 'MadCow.bat'; $s.Arguments = $env:MADCOW_ARGS;" ^
  "$s.WorkingDirectory = $env:MADCOW_DIR; $s.WindowStyle = 7; $s.Description = 'MadCow Charts'; $s.Save()"
if errorlevel 1 goto :error
echo Listo: MadCow arrancara minimizado cada vez que inicies sesion en Windows.
echo Para abrirlo en el navegador: http://localhost:8000  o doble clic en MadCow.bat
pause
exit /b 0

:error
echo No se pudo crear el acceso directo en la carpeta Inicio.
pause
exit /b 1
