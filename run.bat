@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .env (
  echo Нет файла .env. Скопируйте .env.example в .env и заполните его.
  pause
  exit /b 1
)
if not exist .venv (
  echo Первый запуск: ставлю зависимости...
  python -m venv .venv || (echo Не найден Python. Установите его с python.org & pause & exit /b 1)
)
.venv\Scripts\python -m pip install -q -r requirements.txt
.venv\Scripts\python -m zakup_bot
pause
