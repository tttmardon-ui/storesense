@echo off
chcp 65001 >nul
REM Zapusk StoreSense na Windows (nuzhen Docker Desktop)
cd /d "%~dp0"
if not exist .env copy .env.example .env >nul
set PORT=8090
for /f "tokens=2 delims==" %%a in ('findstr /b "DASHBOARD_PORT=" .env') do set PORT=%%a
docker compose up -d --build
if errorlevel 1 (
  echo.
  echo Ошибка запуска. Проверь, что Docker Desktop запущен.
  pause
  exit /b 1
)
echo.
echo Готово! Дашборд: http://localhost:%PORT%
timeout /t 5 >nul
start http://localhost:%PORT%
