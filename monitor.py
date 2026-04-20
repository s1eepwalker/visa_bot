#!/usr/bin/env python3
"""
US Visa Slot Monitor — Посольство США в Астане
Мониторит доступные слоты на ais.usvisa-info.com и уведомляет в Telegram
Поддерживает автоматический логин при устаревании сессии
"""

import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime

import httpx
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError
from twilio.rest import Client as TwilioClient

load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", "90"))
CHECK_INTERVAL_MAX = int(os.getenv("CHECK_INTERVAL_MAX", "120"))
CHECK_INTERVAL_DAY_MIN = int(os.getenv("CHECK_INTERVAL_DAY_MIN", "60"))
CHECK_INTERVAL_DAY_MAX = int(os.getenv("CHECK_INTERVAL_DAY_MAX", "70"))
DAY_START_HOUR   = int(os.getenv("DAY_START_HOUR", "7"))
DAY_END_HOUR     = int(os.getenv("DAY_END_HOUR", "23"))  # верхняя граница НЕ включается
ERROR_ALERT_THRESHOLD = int(os.getenv("ERROR_ALERT_THRESHOLD", "5"))
SCHEDULE_ID      = os.getenv("SCHEDULE_ID", "")
MAX_DATE         = os.getenv("MAX_DATE", "")
EMAIL            = os.getenv("EMAIL", "")
PASSWORD         = os.getenv("PASSWORD", "")

# Twilio — звонок при появлении слотов (опционально)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")  # номер Twilio (формат +1...)
TWILIO_TO_NUMBER   = os.getenv("TWILIO_TO_NUMBER", "")    # ваш номер (формат +7...)

BASE_URL    = "https://ais.usvisa-info.com/ru-kz/niv"
SIGN_IN_URL = f"{BASE_URL}/users/sign_in"
DAYS_URL    = f"{BASE_URL}/schedule/{SCHEDULE_ID}/appointment/days/134.json?appointments[expedite]=false"
TIMES_URL   = f"{BASE_URL}/schedule/{SCHEDULE_ID}/appointment/times/134.json?date={{date}}&appointments[expedite]=false"
BOOKING_URL = f"{BASE_URL}/schedule/{SCHEDULE_ID}/appointment"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def save_cookies(cookies: dict):
    with open("cookies.json", "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)


def load_cookies() -> dict:
    if os.path.exists("cookies.json"):
        try:
            with open("cookies.json", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def build_headers(cookies: dict) -> dict:
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items() if k != "_csrf_token")
    csrf = cookies.get("_csrf_token", "")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
        "Referer": BOOKING_URL,
        "X-Requested-With": "XMLHttpRequest",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "Cookie": cookie_str,
    }
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return headers


async def do_login(client: httpx.AsyncClient) -> dict:
    if not EMAIL or not PASSWORD:
        log.error("EMAIL и PASSWORD не заданы в .env — автологин невозможен")
        return {}

    log.info("Попытка автологина...")
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    base_headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
    }
    try:
        # Получаем страницу логина и CSRF токен (httpx сам ведёт cookie jar)
        resp = await client.get(SIGN_IN_URL, headers=base_headers, timeout=30)
        match = re.search(r'name="csrf-token"\s+content="([^"]+)"', resp.text)
        if not match:
            match = re.search(r'content="([^"]+)"\s+name="csrf-token"', resp.text)
        if not match:
            log.error(f"Не найден csrf-token. Статус: {resp.status_code}, размер: {len(resp.text)}")
            return {}

        auth_token = match.group(1)

        # Извлекаем action формы — locale-prefix может отличаться от ru-kz
        form_match = re.search(r'<form[^>]*action="([^"]*sign_in[^"]*)"', resp.text)
        post_url = form_match.group(1) if form_match else SIGN_IN_URL
        if post_url.startswith("/"):
            post_url = "https://ais.usvisa-info.com" + post_url
        log.info(f"Form action: {post_url}")

        # Отправляем форму логина — без follow_redirects, чтобы видеть реальный ответ
        login_resp = await client.post(
            post_url,
            headers={
                **base_headers,
                "Accept": "text/javascript, application/javascript, application/ecmascript, application/x-ecmascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": auth_token,
                "Origin": "https://ais.usvisa-info.com",
                "Referer": SIGN_IN_URL,
            },
            data={
                "utf8": "✓",
                "user[email]": EMAIL,
                "user[password]": PASSWORD,
                "user[request_locale]": "ru-kz",
                "authenticity_token": auth_token,
                "policy_confirmed": "1",
                "commit": "Sign In",
            },
            timeout=30,
            follow_redirects=False,
        )
        log.info(f"Login POST: статус={login_resp.status_code}, location={login_resp.headers.get('location','-')}")
        if login_resp.status_code not in (200, 302):
            snippet = login_resp.text[:400].replace("\n", " ")
            log.error(f"Автологин не удался. Статус {login_resp.status_code}. Тело: {snippet}")
            return {}

        # Проверяем успех логина: запрашиваем защищённую страницу
        appt_resp = await client.get(BOOKING_URL, headers=base_headers, timeout=30)
        if "sign_in" in str(appt_resp.url) or "user_email" in appt_resp.text:
            snippet = login_resp.text[:400].replace("\n", " ")
            log.error(f"Автологин не удался — после POST всё ещё разлогинены. JS-ответ: {snippet}")
            return {}

        # cookie jar httpx уже содержит все сессионные куки
        new_cookies = {c.name: c.value for c in client.cookies.jar}
        csrf_match = re.search(r'name="csrf-token"\s+content="([^"]+)"', appt_resp.text)
        if csrf_match:
            new_cookies["_csrf_token"] = csrf_match.group(1)
        for k, v in appt_resp.cookies.items():
            new_cookies[k] = v

        save_cookies(new_cookies)
        log.info("Автологин успешен! Cookies обновлены.")
        return new_cookies

    except Exception as e:
        log.error(f"Ошибка при автологине: {e}")
        return {}


async def fetch_available_days(client, cookies, bot, max_date: str = MAX_DATE):
    """Возвращает (dates, cookies). dates=None означает ошибку (а не пустой календарь)."""
    for attempt in range(2):
        try:
            resp = await client.get(DAYS_URL, headers=build_headers(cookies), timeout=30, follow_redirects=False)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if attempt == 0:
                wait = random.randint(5, 10)
                log.warning(f"Сетевой сбой ({type(e).__name__}), ретрай через {wait}с")
                await asyncio.sleep(wait)
                continue
            log.error(f"Сетевая ошибка после ретрая: {e}")
            return None, cookies
        except Exception as e:
            log.error(f"Ошибка при запросе дней: {e}")
            return None, cookies

        if resp.status_code in (401, 302):
            log.warning("Сессия устарела, выполняю автологин...")
            await send_telegram(bot, "🔄 Сессия устарела, выполняю автологин...")
            new_cookies = await do_login(client)
            if not new_cookies:
                await send_telegram(bot, "❌ Автологин не удался. Обновите EMAIL/PASSWORD в .env")
                return None, cookies
            await send_telegram(bot, "✅ Автологин успешен, сессия восстановлена")
            cookies = new_cookies
            continue  # повторяем запрос с новыми куками

        if resp.status_code == 429:
            log.warning("Слишком много запросов (429). Увеличьте CHECK_INTERVAL.")
            return None, cookies

        if 500 <= resp.status_code < 600:
            if attempt == 0:
                wait = random.randint(5, 10)
                log.warning(f"HTTP {resp.status_code}, ретрай через {wait}с")
                await asyncio.sleep(wait)
                continue
            log.error(f"HTTP {resp.status_code} после ретрая")
            return None, cookies

        if resp.status_code != 200:
            log.error(f"Неожиданный HTTP статус: {resp.status_code}")
            return None, cookies

        try:
            data = resp.json()
        except Exception as e:
            log.error(f"Не удалось распарсить JSON ответа: {e}")
            return None, cookies

        dates = [d["date"] for d in data if d.get("business_day", False)]
        if max_date:
            dates = [d for d in dates if d <= max_date]
        return dates, cookies

    return None, cookies


async def fetch_times_for_date(client, cookies, date):
    try:
        resp = await client.get(TIMES_URL.format(date=date), headers=build_headers(cookies), timeout=30)
        resp.raise_for_status()
        return resp.json().get("available_times", [])
    except Exception as e:
        log.error(f"Ошибка при запросе времён для {date}: {e}")
        return []


async def send_telegram(bot: Bot, message: str):
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="HTML", disable_web_page_preview=True)
        log.info("Telegram уведомление отправлено")
    except TelegramError as e:
        log.error(f"Ошибка Telegram: {e}")


def send_twilio_sms(dates: list[str]):
    """Отправляет SMS через Twilio с датами слотов."""
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, TWILIO_TO_NUMBER]):
        return
    try:
        twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        dates_text = ", ".join(dates[:3])
        twilio.messages.create(
            to=TWILIO_TO_NUMBER,
            from_=TWILIO_FROM_NUMBER,
            body=f"🚨 СЛОТЫ НА ВИЗУ! Даты: {dates_text}. Откройте Telegram для деталей.",
        )
        log.info("Twilio SMS отправлено на %s", TWILIO_TO_NUMBER)
    except Exception as e:
        log.error("Ошибка Twilio SMS: %s", e)


async def main():
    if not TELEGRAM_TOKEN:
        log.error("Задайте TELEGRAM_TOKEN в .env!")
        return
    if not SCHEDULE_ID:
        log.error("Задайте SCHEDULE_ID в .env!")
        return

    bot = Bot(token=TELEGRAM_TOKEN)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        cookies = load_cookies()
        if not cookies:
            log.info("Cookies не найдены, пробую автологин...")
            cookies = await do_login(client)
            if not cookies:
                log.error("Не удалось получить cookies. Проверьте EMAIL и PASSWORD в .env")
                return

        log.info(
            "Монитор запущен. Ночь %d-%d сек, день (%d:00-%d:00) %d-%d сек...",
            CHECK_INTERVAL_MIN, CHECK_INTERVAL_MAX,
            DAY_START_HOUR, DAY_END_HOUR,
            CHECK_INTERVAL_DAY_MIN, CHECK_INTERVAL_DAY_MAX,
        )
        await send_telegram(
            bot,
            "🤖 <b>Монитор визы США (Астана) запущен</b>\n"
            f"День ({DAY_START_HOUR:02d}:00-{DAY_END_HOUR:02d}:00): {CHECK_INTERVAL_DAY_MIN}-{CHECK_INTERVAL_DAY_MAX} сек\n"
            f"Ночь: {CHECK_INTERVAL_MIN}-{CHECK_INTERVAL_MAX} сек\n"
            + (f"Ищу слоты до: {MAX_DATE}" if MAX_DATE else "Ищу любые доступные слоты"),
        )

        last_known_dates: set = set()
        check_count = 0
        consecutive_errors = 0
        blind_alert_sent = False

        while True:
            check_count += 1
            log.info(f"[#{check_count}] {datetime.now().strftime('%H:%M:%S')} — проверяю...")

            is_full_check = check_count % 100 == 0

            if is_full_check:
                # Полная проверка без фильтра по дате — только Telegram, без SMS,
                # без обновления last_known_dates (не мешаем основному циклу).
                all_dates, cookies = await fetch_available_days(client, cookies, bot, max_date="")
                if all_dates is None:
                    log.warning(f"Полная проверка #{check_count}: ошибка запроса, пропускаю")
                else:
                    sorted_all = sorted(all_dates)
                    limit_note = f" (фильтр обычных проверок: до {MAX_DATE})" if MAX_DATE else ""
                    if sorted_all:
                        dates_block = "\n".join(f"  📅 {d}" for d in sorted_all)
                        msg = (
                            f"✅ <b>Полная проверка #{check_count}</b> "
                            f"({datetime.now().strftime('%d.%m.%Y %H:%M')}){limit_note}\n"
                            f"Все доступные даты ({len(sorted_all)}):\n{dates_block}"
                        )
                    else:
                        msg = (
                            f"✅ <b>Полная проверка #{check_count}</b> "
                            f"({datetime.now().strftime('%d.%m.%Y %H:%M')}){limit_note}\n"
                            "Свободных слотов нет вообще."
                        )
                    await send_telegram(bot, msg)
                    log.info(f"Полная проверка: всего дат {len(sorted_all)}")
            else:
                available, cookies = await fetch_available_days(client, cookies, bot)

                if available is None:
                    consecutive_errors += 1
                    log.warning(f"Ошибка запроса (подряд: {consecutive_errors}). Состояние не сбрасываю.")
                    if consecutive_errors >= ERROR_ALERT_THRESHOLD and not blind_alert_sent:
                        await send_telegram(
                            bot,
                            f"⚠️ <b>Бот ослеп</b>: {consecutive_errors} ошибок подряд.\n"
                            "Сайт usvisa-info недоступен или сессия не восстанавливается.",
                        )
                        blind_alert_sent = True
                else:
                    if blind_alert_sent:
                        await send_telegram(bot, "✅ Связь с сайтом восстановлена")
                        blind_alert_sent = False
                    consecutive_errors = 0

                    new_dates = set(available) - last_known_dates

                    if new_dates:
                        details = []
                        for date in sorted(new_dates)[:3]:
                            times = await fetch_times_for_date(client, cookies, date)
                            time_str = ", ".join(times[:5]) if times else "нет данных"
                            if len(times) > 5:
                                time_str += f" (+{len(times)-5} ещё)"
                            # Прямая ссылка с датой/временем в query — usvisa-info подхватит как hint
                            first_time = times[0] if times else ""
                            deep_link = (
                                f"{BOOKING_URL}?"
                                f"appointments[consulate_appointment][facility_id]=134&"
                                f"appointments[consulate_appointment][date]={date}&"
                                f"appointments[consulate_appointment][time]={first_time}"
                            )
                            details.append(f"  📅 <a href='{deep_link}'><b>{date}</b></a>: {time_str}")

                        extra = f"\n  ...и ещё {len(new_dates)-3} дат" if len(new_dates) > 3 else ""
                        msg = (
                            "🚨 <b>СЛОТЫ ПОЯВИЛИСЬ!</b>\n\n"
                            + "\n".join(details) + extra
                            + f"\n\n🔗 <a href='{BOOKING_URL}'>Забронировать сейчас →</a>\n\n"
                            "⚡️ Действуйте быстро — слоты разбирают за минуты!"
                        )
                        await send_telegram(bot, msg)
                        send_twilio_sms(sorted(new_dates))
                        log.info(f"Найдены новые слоты: {sorted(new_dates)}")
                    elif available:
                        log.info(f"Слоты уже известны ({len(available)} дат), ждём новых...")
                    else:
                        log.info("Свободных слотов нет.")

                    last_known_dates = set(available)

            hour = datetime.now().hour
            if DAY_START_HOUR <= hour < DAY_END_HOUR:
                interval = random.randint(CHECK_INTERVAL_DAY_MIN, CHECK_INTERVAL_DAY_MAX)
            else:
                interval = random.randint(CHECK_INTERVAL_MIN, CHECK_INTERVAL_MAX)
            log.info(f"Следующая проверка через {interval} сек")
            await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
