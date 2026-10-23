#!/usr/bin/env bash
# Запуск StoreSense на Linux / macOS
set -e
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
docker compose up -d --build
echo
echo "Готово! Дашборд: http://localhost:$(grep -E '^DASHBOARD_PORT=' .env | cut -d= -f2 || echo 8090)"
echo "Логи: docker compose logs -f"
