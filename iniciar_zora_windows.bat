@echo off
chcp 65001 >nul
title Zora - asistente familiar
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python no esta instalado o no esta en el PATH.
  echo Descargalo gratis de https://www.python.org/downloads/
  echo y marca "Add python.exe to PATH" durante la instalacion.
  pause
  exit /b 1
)

echo ============================================
echo   Zora esta arrancando...
echo   Se abrira tu navegador en unos segundos.
echo   Deja esta ventana abierta mientras usas Zora.
echo ============================================
echo.

set PYTHONIOENCODING=utf-8
start "Zora backend" /min cmd /c "python zora_backend.py"
timeout /t 3 /nobreak >nul
start "" http://localhost:8000

echo Zora corre en segundo plano (ventana minimizada "Zora backend").
echo Para cerrarla: cierra esa ventana o usa el Administrador de tareas.
pause
