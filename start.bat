@echo off
chcp 65001 >nul
title AIChatBot - Telegram
cd /d "%~dp0"
if not exist .env (
  echo [!] Нет файла .env. Скопируйте .env.example в .env и впишите ключи.
  pause
  exit /b 1
)
if not exist .venv (
  echo Первый запуск: устанавливаю библиотеки, 1-3 минуты...
  python -m venv .venv || (echo [!] Python не найден. Установите его с python.org, галочка Add Python to PATH & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (echo [!] Ошибка установки библиотек & pause & exit /b 1)
)
".venv\Scripts\python.exe" telegram_bot.py
pause
