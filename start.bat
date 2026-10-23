@echo off
REM Запуск StoreSense на Windows (нужен Docker Desktop)
chcp 65001 >nul
cd /d "%~dp0"
if not exist .env copy .env.example .env >nul
docker compose up -d --build
if errorlevel 1 (
  echo.
  echo Ошибка запуска. Проверь, что Docker Desktop запущен.
  pause
  exit /b 1
)
echo.
echo Готово! Открываю дашборд...
timeout /t 5 >nul
start http://localhost:8080
