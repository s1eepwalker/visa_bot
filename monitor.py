#!/usr/bin/env python3
"""
US Visa Slot Monitor — Посольство США в Астане
Мониторит доступные слоты на ais.usvisa-info.com и уведомляет в Telegram
Поддерживает автоматический логин при устаревании сессии
"""

import asyncio
import html
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone, date as date_cls

import httpx
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError
from twilio.rest import Client as TwilioClient

load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# Часовой пояс Астаны: Казахстан с 01.03.2024 — единый UTC+5, без перехода на лето.
ASTANA_TZ = timezone(timedelta(hours=5))

# Расписание интервалов проверки по времени Астаны.
# (метка, час_начала, час_конца, мин_сек, макс_сек); час_конца НЕ включается.
INTERVAL_SCHEDULE = [
    ("утро",  8, 11, 30, 35),   # 08:00–11:00
    ("день",  11, 12, 60, 65),  # 11:00–12:00
    ("вечер", 12, 20, 65, 90),  # 12:00–20:00
]
NIGHT_INTERVAL = (90, 100)      # всё остальное время (20:00–08:00)

# Backoff при HTTP 429 (Too Many Requests): сервер просит снизить темп.
# Пауза удваивается на каждый следующий 429 подряд, сбрасывается на первом успехе.
RATE_LIMIT_BASE = 300           # первая пауза, сек
RATE_LIMIT_MAX = 3600           # потолок паузы, сек

_rate_limit_strikes = 0         # сколько 429 получено подряд
_pending_backoff = 0            # пауза, которую главный цикл должен отработать


def note_rate_limit() -> int:
    """Регистрирует 429 и возвращает длительность паузы (сек)."""
    global _rate_limit_strikes, _pending_backoff
    _rate_limit_strikes += 1
    _pending_backoff = min(RATE_LIMIT_BASE * 2 ** (_rate_limit_strikes - 1), RATE_LIMIT_MAX)
    return _pending_backoff


def clear_rate_limit() -> bool:
    """Сбрасывает счётчик 429. True, если backoff был активен (значит, лимит снят)."""
    global _rate_limit_strikes, _pending_backoff
    was_active = _rate_limit_strikes > 0
    _rate_limit_strikes = 0
    _pending_backoff = 0
    return was_active


def take_backoff() -> int:
    """Забирает накопленную паузу (0, если её нет). Повторный вызов вернёт 0."""
    global _pending_backoff
    pause, _pending_backoff = _pending_backoff, 0
    return pause


def pick_interval():
    """Возвращает (метка, интервал_сек) по текущему часу Астаны."""
    hour = datetime.now(ASTANA_TZ).hour
    for label, start, end, lo, hi in INTERVAL_SCHEDULE:
        if start <= hour < end:
            return label, random.randint(lo, hi)
    return "ночь", random.randint(*NIGHT_INTERVAL)


def schedule_summary():
    parts = [f"{label} {s:02d}:00-{e:02d}:00 → {lo}-{hi}с"
             for label, s, e, lo, hi in INTERVAL_SCHEDULE]
    parts.append(f"ночь → {NIGHT_INTERVAL[0]}-{NIGHT_INTERVAL[1]}с")
    return " | ".join(parts)
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

# Автобронирование (Фаза 1: только dry-run алерт, без реального POST)
AUTOBOOK_ENABLED   = os.getenv("AUTOBOOK_ENABLED", "false").lower() in ("1", "true", "yes")
AUTOBOOK_RANGES    = os.getenv("AUTOBOOK_RANGES", "")  # "today+2:2026-08-04,2026-09-03:2026-09-10"
AUTOBOOK_DRY_RUN   = os.getenv("AUTOBOOK_DRY_RUN", "true").lower() in ("1", "true", "yes")

BASE_URL    = "https://ais.usvisa-info.com/ru-kz/niv"
SIGN_IN_URL = f"{BASE_URL}/users/sign_in"
DAYS_URL    = f"{BASE_URL}/schedule/{SCHEDULE_ID}/appointment/days/134.json?appointments[expedite]=false"
TIMES_URL   = f"{BASE_URL}/schedule/{SCHEDULE_ID}/appointment/times/134.json?date={{date}}&appointments[expedite]=false"
BOOKING_URL = f"{BASE_URL}/schedule/{SCHEDULE_ID}/appointment"
BOOKED_FILE = "booked.json"

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


def load_booked() -> dict:
    if os.path.exists(BOOKED_FILE):
        try:
            with open(BOOKED_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_booked(date: str, time: str, status: int, location: str):
    payload = {
        "date": date,
        "time": time,
        "booked_at": datetime.now().isoformat(),
        "response_status": status,
        "response_location": location,
    }
    with open(BOOKED_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


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
            pause = note_rate_limit()
            log.warning(
                f"HTTP 429: слишком много запросов (подряд: {_rate_limit_strikes}). "
                f"Пауза {pause} сек перед следующей проверкой."
            )
            await send_telegram(
                bot,
                f"🐌 <b>Сайт ограничил частоту запросов</b> (HTTP 429, подряд: {_rate_limit_strikes})\n"
                f"Пауза {pause // 60} мин перед следующей проверкой.\n"
                "Если повторяется — увеличьте интервалы в INTERVAL_SCHEDULE.",
            )
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

        if clear_rate_limit():
            log.info("Ограничение частоты снято, возвращаюсь к обычному расписанию")
            await send_telegram(bot, "✅ Ограничение частоты снято, вернулся к обычному расписанию")

        dates = [d["date"] for d in data if d.get("business_day", False)]
        if max_date:
            dates = [d for d in dates if d <= max_date]
        return dates, cookies

    return None, cookies


async def fetch_times_for_date(client, cookies, date):
    try:
        resp = await client.get(TIMES_URL.format(date=date), headers=build_headers(cookies), timeout=30)
        if resp.status_code == 429:
            pause = note_rate_limit()
            log.warning(f"HTTP 429 при запросе времён для {date}. Пауза {pause} сек после текущей итерации.")
            return []
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


def parse_autobook_ranges(s: str) -> list[tuple[str, str]]:
    ranges = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            log.warning(f"AUTOBOOK_RANGES: пропущен некорректный диапазон {part!r}")
            continue
        a, b = part.split(":", 1)
        ranges.append((a.strip(), b.strip()))
    return ranges


def resolve_date(token: str, today: date_cls) -> date_cls:
    token = token.strip()
    if token == "today":
        return today
    if token.startswith("today+"):
        return today + timedelta(days=int(token[6:]))
    if token.startswith("today-"):
        return today - timedelta(days=int(token[6:]))
    return datetime.strptime(token, "%Y-%m-%d").date()


def date_in_autobook_ranges(date_str: str, ranges: list[tuple[str, str]], today: date_cls) -> bool:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return False
    for a_tok, b_tok in ranges:
        try:
            a = resolve_date(a_tok, today)
            b = resolve_date(b_tok, today)
        except (ValueError, IndexError) as e:
            log.warning(f"AUTOBOOK: не удалось распарсить диапазон {a_tok}:{b_tok} — {e}")
            continue
        if a <= d <= b:
            return True
    return False


def parse_booking_form(page_html: str) -> tuple[str, dict[str, str]]:
    form_match = re.search(
        r'<form[^>]*action="([^"]*appointment[^"]*)"[^>]*method="post"',
        page_html, re.IGNORECASE,
    )
    if not form_match:
        raise ValueError("Форма бронирования не найдена в HTML")
    action = html.unescape(form_match.group(1))
    if action.startswith("/"):
        action = "https://ais.usvisa-info.com" + action

    form_start = form_match.start()
    end_match = re.search(r'</form>', page_html[form_start:])
    form_html = page_html[form_start:form_start + end_match.end()] if end_match else page_html[form_start:]

    fields: dict[str, str] = {}
    for m in re.finditer(
        r'<input\b[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"',
        form_html,
    ):
        fields[m.group(1)] = html.unescape(m.group(2))
    for m in re.finditer(
        r'<input\b[^>]*name="([^"]+)"[^>]*value="([^"]*)"[^>]*type="hidden"',
        form_html,
    ):
        fields.setdefault(m.group(1), html.unescape(m.group(2)))
    submit = re.search(
        r'<input\b[^>]*type="submit"[^>]*name="(commit)"[^>]*value="([^"]*)"',
        form_html,
    )
    if submit:
        fields[submit.group(1)] = html.unescape(submit.group(2))
    return action, fields


async def do_real_booking(client, cookies: dict, date: str, time: str) -> tuple[bool, int, str]:
    """Реальный POST на бронирование. Возвращает (success, status_code, message)."""
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items() if k != "_csrf_token")
    page_headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
        "Cookie": cookie_str,
    }
    log.info(f"AUTOBOOK: GET формы для {date} {time}")
    try:
        page = await client.get(BOOKING_URL, headers=page_headers, timeout=30)
    except Exception as e:
        return False, 0, f"GET формы упал: {e}"
    if page.status_code != 200:
        return False, page.status_code, f"GET формы вернул {page.status_code}"

    try:
        action, fields = parse_booking_form(page.text)
    except ValueError as e:
        snippet = page.text[:300].replace("\n", " ")
        return False, page.status_code, f"{e}. HTML начало: {snippet}"

    fields["appointments[consulate_appointment][facility_id]"] = "134"
    fields["appointments[consulate_appointment][date]"] = date
    fields["appointments[consulate_appointment][time]"] = time
    fields.setdefault("utf8", "✓")
    fields.setdefault("confirmed_limit_message", "1")
    fields.setdefault("use_consulate_appointment_capacity", "true")

    auth_token = fields.get("authenticity_token", "")
    log.info(f"AUTOBOOK: POST {action}, полей={len(fields)}, csrf={'есть' if auth_token else 'НЕТ'}")

    post_headers = {
        **page_headers,
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://ais.usvisa-info.com",
        "Referer": BOOKING_URL,
        "X-CSRF-Token": auth_token,
    }
    try:
        resp = await client.post(action, headers=post_headers, data=fields, timeout=30, follow_redirects=False)
    except Exception as e:
        return False, 0, f"POST упал: {e}"

    location = resp.headers.get("location", "")
    log.info(f"AUTOBOOK: POST статус={resp.status_code}, location={location}")

    # Успех: 302 на следующий шаг (insurance/confirmation), не назад на форму бронирования
    if resp.status_code in (302, 303) and location:
        if "/appointment/days" in location or location.endswith("/appointment"):
            return False, resp.status_code, f"302 назад на форму ({location}) — бронь не прошла"
        return True, resp.status_code, location
    if resp.status_code == 200:
        snippet = resp.text[:400].replace("\n", " ")
        return False, 200, f"200 (форма перерисована, ошибка валидации). Тело: {snippet}"
    snippet = resp.text[:200].replace("\n", " ") if resp.text else ""
    return False, resp.status_code, f"HTTP {resp.status_code}. Location={location or '-'}. Тело: {snippet}"


async def try_autobook(client, cookies: dict, bot, date: str, time: str) -> bool:
    deep_link = (
        f"{BOOKING_URL}?"
        f"appointments[consulate_appointment][facility_id]=134&"
        f"appointments[consulate_appointment][date]={date}&"
        f"appointments[consulate_appointment][time]={time}"
    )
    if AUTOBOOK_DRY_RUN:
        await send_telegram(
            bot,
            f"🤖 <b>AUTOBOOK CANDIDATE</b> (dry-run)\n"
            f"  📅 <a href='{deep_link}'>{date} {time}</a>\n"
            f"  Реальный POST отключён. Перейди по ссылке и забронируй вручную.",
        )
        log.info(f"AUTOBOOK CANDIDATE (dry-run): {date} {time}")
        return False

    log.info(f"AUTOBOOK LIVE: пытаюсь забронировать {date} {time}")
    success, status, message = await do_real_booking(client, cookies, date, time)
    if success:
        save_booked(date, time, status, message)
        await send_telegram(
            bot,
            f"✅ <b>AUTOBOOK УСПЕШНО</b>\n"
            f"  📅 <b>{date} {time}</b>\n"
            f"  HTTP {status} → {message}\n\n"
            f"⚠️ <b>СРОЧНО открой личный кабинет и проверь</b> — возможно нужно подтвердить страховку/оплату.\n"
            f"🔗 <a href='{BOOKING_URL}'>Перейти к записи →</a>",
        )
        return True
    await send_telegram(
        bot,
        f"❌ <b>AUTOBOOK НЕУДАЧА</b>\n"
        f"  📅 {date} {time}\n"
        f"  HTTP {status}: {message[:300]}\n\n"
        f"🔗 <a href='{deep_link}'>Забронируй вручную →</a>",
    )
    return False


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

        log.info("Монитор запущен. Расписание (Астана): %s", schedule_summary())
        autobook_ranges = parse_autobook_ranges(AUTOBOOK_RANGES) if AUTOBOOK_ENABLED else []
        booked_state = load_booked()
        if AUTOBOOK_ENABLED and booked_state:
            log.warning(
                f"booked.json найден ({booked_state.get('date')} {booked_state.get('time')}), "
                "autobook отключён до удаления файла."
            )
            autobook_ranges = []
            autobook_status = (
                f"\n🤖 Autobook: <b>ОТКЛЮЧЁН</b> — booked.json: "
                f"{booked_state.get('date')} {booked_state.get('time')}"
            )
        elif AUTOBOOK_ENABLED:
            today = datetime.now().date()
            preview = "; ".join(
                f"{resolve_date(a, today)}…{resolve_date(b, today)}"
                for a, b in autobook_ranges
            ) or "(пусто)"
            log.info(f"Autobook включён, dry-run={AUTOBOOK_DRY_RUN}, диапазоны: {preview}")
            autobook_status = (
                f"\n🤖 Autobook: <b>{'DRY-RUN' if AUTOBOOK_DRY_RUN else 'LIVE'}</b>, "
                f"диапазоны: {preview}"
            )
        else:
            autobook_status = ""

        await send_telegram(
            bot,
            "🤖 <b>Монитор визы США (Астана) запущен</b>\n"
            f"Расписание (время Астаны, UTC+5):\n{schedule_summary()}\n"
            + (f"Ищу слоты до: {MAX_DATE}" if MAX_DATE else "Ищу любые доступные слоты")
            + autobook_status,
        )

        last_known_dates: set = set()
        last_all_count = -1          # -1 => первая успешная проверка всегда шлёт полный список
        check_count = 0
        consecutive_errors = 0
        blind_alert_sent = False

        while True:
            check_count += 1
            log.info(f"[#{check_count}] {datetime.now().strftime('%H:%M:%S')} — проверяю...")

            is_periodic_report = check_count % 100 == 0

            # MAX_DATE фильтруется локально, поэтому берём полный список одним запросом
            # и уже из него получаем отфильтрованный для основной логики.
            all_dates, cookies = await fetch_available_days(client, cookies, bot, max_date="")
            available = None if all_dates is None else (
                [d for d in all_dates if d <= MAX_DATE] if MAX_DATE else list(all_dates)
            )

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
                    times_cache: dict[str, list[str]] = {}

                    if autobook_ranges:
                        today = datetime.now().date()
                        candidates = sorted(
                            d for d in new_dates
                            if date_in_autobook_ranges(d, autobook_ranges, today)
                        )
                        for date in candidates:
                            times_cache[date] = await fetch_times_for_date(client, cookies, date)
                            if not times_cache[date]:
                                log.warning(f"AUTOBOOK: дата {date} в диапазоне, но слотов нет")
                                continue
                            booked = await try_autobook(client, cookies, bot, date, times_cache[date][0])
                            if booked:
                                autobook_ranges = []
                                log.info("AUTOBOOK: бронь успешна, дальнейшие попытки отключены")
                                break

                    details = []
                    for date in sorted(new_dates)[:3]:
                        times = times_cache.get(date) or await fetch_times_for_date(client, cookies, date)
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

                # Полный список без фильтра MAX_DATE: сразу при росте числа окон,
                # плюс периодически как heartbeat.
                all_grew = len(all_dates) > last_all_count
                if all_grew or is_periodic_report:
                    if last_all_count < 0:
                        reason = "первая проверка после запуска"
                    elif all_grew:
                        reason = f"окон стало больше: {last_all_count} → {len(all_dates)}"
                    else:
                        reason = f"плановая сводка #{check_count}"
                    sorted_all = sorted(all_dates)
                    header = (
                        f"{'📈' if all_grew else '✅'} <b>Все доступные даты</b> "
                        f"({datetime.now().strftime('%d.%m.%Y %H:%M')})\n"
                        f"<i>{reason}</i>\n"
                    )
                    if sorted_all:
                        limit_note = (
                            f"\n<i>Обычные алерты фильтруются до {MAX_DATE}</i>" if MAX_DATE else ""
                        )
                        dates_block = "\n".join(
                            f"  {'🆕' if d in new_dates else '📅'} {d}" for d in sorted_all
                        )
                        await send_telegram(
                            bot,
                            header + f"Всего: {len(sorted_all)}\n{dates_block}" + limit_note,
                        )
                    else:
                        await send_telegram(bot, header + "Свободных слотов нет вообще.")
                    log.info(f"Отправлен полный список ({reason}): всего дат {len(all_dates)}")

                last_all_count = len(all_dates)

            backoff = take_backoff()
            if backoff:
                log.warning(f"Backoff после 429: следующая проверка через {backoff} сек")
                await asyncio.sleep(backoff)
                continue

            label, interval = pick_interval()
            log.info(f"Следующая проверка через {interval} сек ({label}, Астана)")
            await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
