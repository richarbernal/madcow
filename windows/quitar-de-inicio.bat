@echo off
rem Deshace arrancar-con-windows.bat: MadCow deja de arrancar al iniciar sesion.
setlocal
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\MadCow.lnk"
if not exist "%LNK%" goto :none
del "%LNK%" || goto :none
echo Listo: MadCow ya no arrancara al iniciar sesion en Windows.
pause
exit /b 0

:none
echo MadCow no estaba configurado para arrancar con Windows.
pause
exit /b 0
