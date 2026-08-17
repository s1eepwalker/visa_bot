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
- `AUTOBOOK_ENABLED` — `true`/`false`, включает проверку диапазонов автобронирования
- `AUTOBOOK_RANGES` — список `from:to`, разделённых запятыми. `from`/`to` — `YYYY-MM-DD` или относительный токен `today`/`today+N`/`today-N`. Пример: `today+2:2026-08-04,2026-09-03:2026-09-10,2026-09-28:2026-12-31`
- `AUTOBOOK_DRY_RUN` — `true` (по умолчанию) шлёт только Telegram-алерт `AUTOBOOK CANDIDATE` без реального POST. `false` — реальная бронь: `do_real_booking()` GET-ит форму с `BOOKING_URL`, парсит скрытые поля + CSRF, постит с заполненными `appointments[consulate_appointment][date|time|facility_id]`. Успех = 302 на следующий шаг (страховка/подтверждение), неудача = 200 с перерисованной формой. После успеха пишется `booked.json` и autobook отключается до удаления файла (защита от двойной брони). Если форма посольства потребует `appointments[asc_appointment][...]` — первая попытка упадёт, тело ответа уйдёт в Telegram и в лог

## Архитектура

Один файл `monitor.py` — асинхронный цикл на `httpx.AsyncClient` + `python-telegram-bot`.

Поток данных:
1. `load_cookies()` читает `cookies.json`. При отсутствии — `do_login()` парсит CSRF-токен из HTML страницы `/users/sign_in`, постит форму, собирает куки и сохраняет.
2. Каждый цикл `fetch_available_days()` дёргает `…/appointment/days/134.json`. На `401`/`302` автоматически вызывает `do_login()` и повторяет запрос. На `429` — Telegram-алерт + экспоненциальный backoff (`note_rate_limit()`: 300с, далее удвоение до потолка 3600с), который главный цикл отрабатывает вместо обычного интервала; первый успешный ответ сбрасывает счётчик (`clear_rate_limit()`) и шлёт сообщение о восстановлении.
3. Сравнение `set(available) - last_known_dates` — уведомление в Telegram только о **новых** датах (чтобы не спамить).
4. Для первых 3 новых дат подгружаются конкретные слоты через `fetch_times_for_date()` (`…/times/134.json?date=…`).
5. Если `AUTOBOOK_ENABLED=true` и новая дата попадает в `AUTOBOOK_RANGES` — вызывается `try_autobook()`. В dry-run только шлётся Telegram-алерт; в live-режиме `do_real_booking()` парсит форму бронирования и делает POST. На успех создаётся `booked.json` и autobook отключается на всю сессию + при следующих рестартах (пока файл не удалят).
6. Полный список дат **без фильтра `MAX_DATE`** уходит в Telegram, когда общее число окон выросло относительно прошлой проверки (`last_all_count`), а также раз в 100 итераций как heartbeat. Отдельного HTTP-запроса для этого нет: `MAX_DATE` применяется локально, поэтому цикл делает один запрос с `max_date=""` и фильтрует список в памяти.

Эндпоинт `134` в URL — это facility ID посольства Астаны, зашит в константах `DAYS_URL`/`TIMES_URL` (`monitor.py:32-34`).

`build_headers()` отделяет специальный ключ `_csrf_token` от остальных кук: он уходит в заголовок `X-CSRF-Token`, а не в `Cookie`. Это важно при ручном редактировании `cookies.json`.

`check.py` — одноразовый отладочный скрипт для проверки парсинга CSRF, не часть рантайма.
