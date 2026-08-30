#!/usr/bin/env python3
"""
US Visa Slot Monitor — Посольство США в Астане
Мониторит доступные слоты на ais.usvisa-info.com и уведомляет в Telegram
Поддерживает автоматический логин при устаревании сессии
"""

import asyncio
import contextlib
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
BROWSER_UA  = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")

# Сколько раз повторить запрос при 5xx/сетевом сбое, прежде чем пропустить итерацию.
# Раньше ретрай был один и каждый 502 стоил целой проверки (≈30 мин слепоты в сутки).
TRANSIENT_RETRIES = int(os.getenv("TRANSIENT_RETRIES", "3"))
TRANSIENT_BACKOFF = (2, 4, 7)  # базовая пауза по номеру попытки, сверху джиттер 0-2с

# Прогрев формы бронирования: сколько секунд считать разобранную форму актуальной
FORM_CACHE_TTL = int(os.getenv("FORM_CACHE_TTL", "600"))

# times.json умеет возвращать пустой список на дату, которую days.json уже показывает
# (разные кеши на стороне сайта) — поэтому пустой ответ переспрашиваем
TIMES_RETRIES    = int(os.getenv("TIMES_RETRIES", "2"))
TIMES_RETRY_WAIT = int(os.getenv("TIMES_RETRY_WAIT", "3"))

# Выходные каналы: проверки идут по кругу через разные внешние IP (SOCKS-туннели),
# поэтому каждый адрес сохраняет прежний спокойный ритм, а суммарная частота растёт
# кратно их числу. "direct" — без прокси, с самого VPS. Сессия к IP не привязана,
# поэтому куки у всех каналов общие.
WORKER_PROXIES = [p.strip() for p in os.getenv("WORKER_PROXIES", "direct").split(",") if p.strip()]
MIN_STEP_SECONDS = int(os.getenv("MIN_STEP_SECONDS", "10"))  # нижняя граница шага цикла

# Раз во сколько проверок слать в Telegram полный список дат как heartbeat.
# С ротацией по каналам проверок стало кратно больше, и на 100 сводки шли слишком часто.
REPORT_EVERY = int(os.getenv("REPORT_EVERY", "500"))

# Минимальный запас по времени до даты собеседования: на окно «завтра» физически
# не успеть доехать, поэтому такие даты автобронь пропускает, даже если они попадают
# в AUTOBOOK_RANGES. Границы диапазонов заданы абсолютными датами, и когда сегодняшнее
# число войдёт внутрь диапазона, без этого фильтра кандидатом стал бы и завтрашний день.
AUTOBOOK_MIN_LEAD_DAYS = int(os.getenv("AUTOBOOK_MIN_LEAD_DAYS", "2"))

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


def cookie_header(cookies: dict) -> str:
    """Cookie-строка без служебного ключа _csrf_token (он уходит в заголовок)."""
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if k != "_csrf_token")


def booking_page_headers(cookies: dict) -> dict:
    """Заголовки для HTML-страницы бронирования (не XHR, поэтому Accept: text/html)."""
    return {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
        "Cookie": cookie_header(cookies),
    }


def build_headers(cookies: dict) -> dict:
    cookie_str = cookie_header(cookies)
    csrf = cookies.get("_csrf_token", "")
    headers = {
        "User-Agent": BROWSER_UA,
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
    transient = 0     # израсходованные ретраи на 5xx/сетевых сбоях
    relogged = False  # автологин в рамках одного вызова делаем максимум один раз

    def transient_wait() -> int:
        base = TRANSIENT_BACKOFF[min(transient, len(TRANSIENT_BACKOFF) - 1)]
        return base + random.randint(0, 2)

    while True:
        try:
            resp = await client.get(DAYS_URL, headers=build_headers(cookies), timeout=30, follow_redirects=False)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if transient < TRANSIENT_RETRIES:
                wait = transient_wait()
                transient += 1
                log.warning(
                    f"Сетевой сбой ({type(e).__name__}), ретрай {transient}/{TRANSIENT_RETRIES} через {wait}с"
                )
                await asyncio.sleep(wait)
                continue
            log.error(f"Сетевая ошибка после {transient} ретраев: {e}")
            return None, cookies
        except Exception as e:
            log.error(f"Ошибка при запросе дней: {e}")
            return None, cookies

        if resp.status_code in (401, 302):
            if relogged:
                log.error("Сессия слетела повторно в рамках одной проверки, пропускаю итерацию")
                return None, cookies
            relogged = True
            log.warning("Сессия устарела, выполняю автологин...")
            await send_telegram(bot, "🔄 Сессия устарела, выполняю автологин...")
            new_cookies = await do_login(client)
            if not new_cookies:
                await send_telegram(bot, "❌ Автологин не удался. Обновите EMAIL/PASSWORD в .env")
                return None, cookies
            await send_telegram(bot, "✅ Автологин успешен, сессия восстановлена")
            cookies = new_cookies
            invalidate_form_cache("сменилась сессия")
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
            if transient < TRANSIENT_RETRIES:
                wait = transient_wait()
                transient += 1
                log.warning(f"HTTP {resp.status_code}, ретрай {transient}/{TRANSIENT_RETRIES} через {wait}с")
                await asyncio.sleep(wait)
                continue
            log.error(f"HTTP {resp.status_code} после {transient} ретраев")
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


async def fetch_times_for_date(client, cookies, date, retries: int = TIMES_RETRIES):
    """Времена на дату. Пустой ответ повторяем: days.json бывает свежее кеша times.json."""
    for attempt in range(retries + 1):
        try:
            resp = await client.get(TIMES_URL.format(date=date), headers=build_headers(cookies), timeout=30)
            if resp.status_code == 429:
                pause = note_rate_limit()
                log.warning(f"HTTP 429 при запросе времён для {date}. Пауза {pause} сек после текущей итерации.")
                return []
            resp.raise_for_status()
            times = resp.json().get("available_times", [])
        except Exception as e:
            log.error(f"Ошибка при запросе времён для {date}: {e}")
            return []

        if times or attempt == retries:
            return times
        log.info(f"times.json для {date} пуст, повтор {attempt + 1}/{retries} через {TIMES_RETRY_WAIT}с")
        await asyncio.sleep(TIMES_RETRY_WAIT)
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


# Прогретая форма бронирования: {"action", "fields", "cookie_sig", "at"}.
# Позволяет при находке слота слать POST сразу, без GET страницы (экономит ~1-1.5с).
_form_cache: dict | None = None


def invalidate_form_cache(reason: str = ""):
    global _form_cache
    if _form_cache is not None:
        _form_cache = None
        log.info(f"Кеш формы брони сброшен{': ' + reason if reason else ''}")


def get_cached_form(cookies: dict):
    """Возвращает (action, fields) из кеша, либо None если кеша нет/протух/сессия сменилась."""
    if not _form_cache:
        return None
    if _form_cache["cookie_sig"] != cookie_header(cookies):
        return None
    if (datetime.now() - _form_cache["at"]).total_seconds() > FORM_CACHE_TTL:
        return None
    return _form_cache["action"], dict(_form_cache["fields"])


async def fetch_booking_form(client, cookies: dict) -> tuple[str, dict]:
    """GET страницы брони + разбор формы, результат кладётся в кеш. RuntimeError при неудаче."""
    global _form_cache
    try:
        page = await client.get(BOOKING_URL, headers=booking_page_headers(cookies), timeout=30)
    except Exception as e:
        raise RuntimeError(f"GET формы упал: {e}") from e
    if page.status_code != 200:
        raise RuntimeError(f"GET формы вернул {page.status_code}")
    try:
        action, fields = parse_booking_form(page.text)
    except ValueError as e:
        snippet = page.text[:300].replace("\n", " ")
        raise RuntimeError(f"{e}. HTML начало: {snippet}") from e
    _form_cache = {
        "action": action,
        "fields": fields,
        "cookie_sig": cookie_header(cookies),
        "at": datetime.now(),
    }
    return action, dict(fields)


async def warm_booking_form(client, cookies: dict):
    """Фоновый прогрев: обновляет кеш формы, если он протух. Ошибки не фатальны."""
    if get_cached_form(cookies):
        return
    try:
        _, fields = await fetch_booking_form(client, cookies)
        log.info(
            f"Прогрев формы брони: ок, полей={len(fields)}, "
            f"csrf={'есть' if fields.get('authenticity_token') else 'НЕТ'}"
        )
    except RuntimeError as e:
        log.warning(f"Прогрев формы брони не удался: {e}")


async def do_real_booking(client, cookies: dict, date: str, time: str) -> tuple[bool, int, str]:
    """Реальный POST на бронирование. Возвращает (success, status_code, message)."""
    page_headers = booking_page_headers(cookies)
    last_status, last_msg = 0, "попытка брони не выполнена"

    # Попытка 0 — с прогретой формой (если есть), попытка 1 — со свежей после сброса кеша
    for attempt in range(2):
        cached = get_cached_form(cookies) if attempt == 0 else None
        if cached:
            action, fields = cached
            log.info(f"AUTOBOOK: форма из прогретого кеша для {date} {time}")
        else:
            log.info(f"AUTOBOOK: GET формы для {date} {time}")
            try:
                action, fields = await fetch_booking_form(client, cookies)
            except RuntimeError as e:
                return False, last_status, str(e)

        fields["appointments[consulate_appointment][facility_id]"] = "134"
        fields["appointments[consulate_appointment][date]"] = date
        fields["appointments[consulate_appointment][time]"] = time
        fields.setdefault("utf8", "✓")
        fields.setdefault("confirmed_limit_message", "1")
        fields.setdefault("use_consulate_appointment_capacity", "true")

        auth_token = fields.get("authenticity_token", "")
        log.info(
            f"AUTOBOOK: POST {action}, полей={len(fields)}, "
            f"csrf={'есть' if auth_token else 'НЕТ'}, форма={'кеш' if cached else 'свежая'}"
        )

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
                last_msg = f"302 назад на форму ({location}) — бронь не прошла"
            else:
                return True, resp.status_code, location
        elif resp.status_code == 200:
            snippet = resp.text[:400].replace("\n", " ")
            last_msg = f"200 (форма перерисована, ошибка валидации). Тело: {snippet}"
        else:
            snippet = resp.text[:200].replace("\n", " ") if resp.text else ""
            last_msg = f"HTTP {resp.status_code}. Location={location or '-'}. Тело: {snippet}"
        last_status = resp.status_code

        # Кешированная форма могла протухнуть (CSRF/скрытые поля) — один повтор со свежей
        if cached:
            invalidate_form_cache("POST не прошёл, повторяю со свежей формой")
            continue
        return False, last_status, last_msg

    return False, last_status, last_msg


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

    async with contextlib.AsyncExitStack() as stack:
        channels = []
        for spec in WORKER_PROXIES:
            proxy = None if spec.lower() in ("direct", "none", "-") else spec
            try:
                client = await stack.enter_async_context(
                    httpx.AsyncClient(follow_redirects=True, proxy=proxy)
                )
            except Exception as e:
                log.error(f"Канал {spec} не поднялся ({e}), пропускаю")
                continue
            channels.append((spec, client))
        if not channels:
            log.error("Ни один выходной канал не доступен — проверьте WORKER_PROXIES")
            return
        log.info("Выходные каналы: %s", ", ".join(spec for spec, _ in channels))

        client = channels[0][1]  # для стартового логина
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
            today = datetime.now(ASTANA_TZ).date()
            preview = "; ".join(
                f"{resolve_date(a, today)}…{resolve_date(b, today)}"
                for a, b in autobook_ranges
            ) or "(пусто)"
            log.info(
                f"Autobook включён, dry-run={AUTOBOOK_DRY_RUN}, "
                f"мин. запас {AUTOBOOK_MIN_LEAD_DAYS} дн., диапазоны: {preview}"
            )
            autobook_status = (
                f"\n🤖 Autobook: <b>{'DRY-RUN' if AUTOBOOK_DRY_RUN else 'LIVE'}</b>, "
                f"не ближе {AUTOBOOK_MIN_LEAD_DAYS} дн., диапазоны: {preview}"
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
        autobook_pending: set = set()  # даты в диапазоне, ждущие появления времён
        last_all_count = -1          # -1 => первая успешная проверка всегда шлёт полный список
        check_count = 0
        consecutive_errors = 0
        blind_alert_sent = False

        while True:
            check_count += 1
            spec, client = channels[(check_count - 1) % len(channels)]
            log.info(
                f"[#{check_count}] {datetime.now().strftime('%H:%M:%S')} — проверяю ({spec})..."
            )

            is_periodic_report = check_count % REPORT_EVERY == 0

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
                times_cache: dict[str, list[str]] = {}

                # Автобронь идёт независимо от уведомлений: кроме новых дат в диапазоне
                # проверяем отложенные — те, что в календаре есть, но times.json на них был
                # пуст. Ёмкость может вернуться в любой момент (чужая отмена), а «новой»
                # такая дата больше никогда не станет.
                if autobook_ranges:
                    today = datetime.now(ASTANA_TZ).date()
                    # ISO-даты сравниваются как строки, поэтому границу держим строкой
                    min_lead = (today + timedelta(days=AUTOBOOK_MIN_LEAD_DAYS)).isoformat()
                    autobook_pending &= set(available)  # выпавшие из календаря забываем

                    # Дата, до которой уже не успеть доехать, годной со временем не станет —
                    # убираем её из очереди совсем, чтобы не перебирать каждую итерацию.
                    stale = {d for d in autobook_pending if d < min_lead}
                    if stale:
                        log.info(
                            f"AUTOBOOK: снимаю с перепроверки {', '.join(sorted(stale))} — "
                            f"ближе чем {AUTOBOOK_MIN_LEAD_DAYS} дн."
                        )
                        autobook_pending -= stale

                    fresh = {d for d in new_dates if date_in_autobook_ranges(d, autobook_ranges, today)}
                    too_soon = {d for d in fresh if d < min_lead}
                    if too_soon:
                        log.info(
                            f"AUTOBOOK: {', '.join(sorted(too_soon))} в диапазоне, но раньше "
                            f"{min_lead} (запас {AUTOBOOK_MIN_LEAD_DAYS} дн.) — не бронирую"
                        )
                    candidates = sorted((fresh - too_soon) | autobook_pending)
                    for date in candidates:
                        times_cache[date] = await fetch_times_for_date(client, cookies, date)
                        if not times_cache[date]:
                            if date not in autobook_pending:
                                log.warning(
                                    f"AUTOBOOK: дата {date} в диапазоне, но слотов нет — "
                                    "беру на перепроверку"
                                )
                            autobook_pending.add(date)
                            continue
                        if date in autobook_pending:
                            log.info(f"AUTOBOOK: на отложенной дате {date} появились времена")
                            autobook_pending.discard(date)
                        booked = await try_autobook(client, cookies, bot, date, times_cache[date][0])
                        if booked:
                            autobook_ranges = []
                            autobook_pending.clear()
                            log.info("AUTOBOOK: бронь успешна, дальнейшие попытки отключены")
                            break

                if new_dates:
                    details = []
                    for date in sorted(new_dates)[:3]:
                        times = times_cache.get(date) or await fetch_times_for_date(client, cookies, date, retries=0)
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

                # Держим форму брони разобранной заранее: при находке слота POST уйдёт
                # сразу, без GET страницы. Актуально только для живого автобронирования.
                if autobook_ranges and not AUTOBOOK_DRY_RUN:
                    await warm_booking_form(client, cookies)

            backoff = take_backoff()
            if backoff:
                log.warning(f"Backoff после 429: следующая проверка через {backoff} сек")
                await asyncio.sleep(backoff)
                continue

            label, interval = pick_interval()
            # interval — ритм ОДНОГО выхода; шаг общего цикла во столько же раз короче,
            # сколько у нас выходных каналов
            step = max(MIN_STEP_SECONDS, round(interval / len(channels)))
            log.info(
                f"Следующая проверка через {step} сек ({label}, Астана; "
                f"на канал раз в ~{interval}с, каналов {len(channels)})"
            )
            await asyncio.sleep(step)


if __name__ == "__main__":
    asyncio.run(main())
