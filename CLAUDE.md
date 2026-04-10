# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Назначение

Бот мониторит свободные слоты на собеседование для визы США в посольстве Астаны (`ais.usvisa-info.com/ru-kz/niv`) и шлёт уведомления в Telegram.

## Команды

```bash
pip install -r requirements.txt   # установка зависимостей
python monitor.py                 # основной запуск (бесконечный цикл)
python update_cookies.py          # ручное обновление cookies.json из строки браузера
```

Тестов нет. Линтеров нет. Логи пишутся в `monitor.log`.

## Конфигурация

Переменные окружения читаются из `.env` (см. `monitor.py:22-28`):
- `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` — обязательные для уведомлений
- `SCHEDULE_ID` — ID записи (из URL `/schedule/<ID>/appointment`), обязателен
- `CHECK_INTERVAL` — секунды между проверками (по умолчанию 90; **не ставить меньше 60** — риск 429/бана)
- `MAX_DATE` — фильтр верхней границы дат (YYYY-MM-DD)
- `EMAIL`, `PASSWORD` — для автологина при истёкшей сессии

## Архитектура

Один файл `monitor.py` — асинхронный цикл на `httpx.AsyncClient` + `python-telegram-bot`.

Поток данных:
1. `load_cookies()` читает `cookies.json`. При отсутствии — `do_login()` парсит CSRF-токен из HTML страницы `/users/sign_in`, постит форму, собирает куки и сохраняет.
2. Каждый цикл `fetch_available_days()` дёргает `…/appointment/days/134.json`. На `401`/`302` автоматически вызывает `do_login()` и повторяет запрос.
3. Сравнение `set(available) - last_known_dates` — уведомление в Telegram только о **новых** датах (чтобы не спамить).
4. Для первых 3 новых дат подгружаются конкретные слоты через `fetch_times_for_date()` (`…/times/134.json?date=…`).
5. Каждые 100 итераций — heartbeat-сообщение в Telegram.

Эндпоинт `134` в URL — это facility ID посольства Астаны, зашит в константах `DAYS_URL`/`TIMES_URL` (`monitor.py:32-34`).

`build_headers()` отделяет специальный ключ `_csrf_token` от остальных кук: он уходит в заголовок `X-CSRF-Token`, а не в `Cookie`. Это важно при ручном редактировании `cookies.json`.

`check.py` — одноразовый отладочный скрипт для проверки парсинга CSRF, не часть рантайма.
