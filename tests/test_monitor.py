import asyncio
import logging
import types
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

import httpx
import pytest
from telegram.error import BadRequest, TimedOut

import monitor
from monitor import ASTANA_TZ


def astana(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ASTANA_TZ)


# --- Расписание: по выходным утреннего окна нет ---------------------------------

def test_saturday_morning_uses_day_interval():
    now = astana(2026, 9, 12, 9)  # суббота
    label, interval = monitor.pick_interval(now)
    assert label == "день"
    assert 45 <= interval <= 48


def test_sunday_from_eight_is_day():
    label, _ = monitor.pick_interval(astana(2026, 9, 13, 8, 5))  # воскресенье
    assert label == "день"


def test_weekday_morning_keeps_morning_interval():
    label, interval = monitor.pick_interval(astana(2026, 9, 14, 9))  # понедельник
    assert label == "утро"
    assert 30 <= interval <= 35


def test_weekend_evening_and_night_unchanged():
    assert monitor.pick_interval(astana(2026, 9, 12, 15))[0] == "вечер"
    assert monitor.pick_interval(astana(2026, 9, 12, 7))[0] == "ночь"


def test_schedule_summary_shows_weekend_separately():
    summary = monitor.schedule_summary()
    assert "выходные" in summary
    assert "день 08:00-12:00" in summary


# --- Урезанные копии живых страниц (снято 11.09.2026, личные данные вырезаны) -------

APPT_PATH = "/ru-kz/niv/schedule/72928759/appointment"


def limit_warning_page(left: int) -> str:
    return f"""<html><head><title>Scheduling Limit Warning | Official U.S. Department of State</title></head>
<body class='appointment new niv'><main id='main'>
<div class='row subTitle'><h1 class='text'>Scheduling Limit Warning</h1></div>
<div class='callout secondary animate bounce-in'> <p>Внимание: Максимальное количество разрешенных
отмен/переносов собеседования на этом сервисе: 3. У вас осталось {left} попыток до достижения этого
лимита. Имейте ввиду, что если вы достигнете этого лимита, ваше собеседование будет заблокировано.</p> </div>
<form action="{APPT_PATH}" accept-charset="UTF-8" method="get"> <div class='row'>
<input type="checkbox" name="confirmed_limit_message" id="confirmed_limit_message" value="1" />
<label style="display: inline" for="confirmed_limit_message">Я понимаю </label><hr> </div>
<a class="button secondary" href="/ru-kz/niv/groups/1">Закрыть</a>
<input type="submit" name="commit" value="Continue" class="button primary" data-disable-with="Continue" />
</form></main>
<input type="radio" name="visa_type" id="visa_type_NIV" value="Неиммиграционная виза" checked="checked" />
</body></html>"""


APPLICANTS_PAGE = f"""<html><body><main id='main'>
<form action="{APPT_PATH}" accept-charset="UTF-8" method="get"> <p> Отметьте всех заявителей,
которым необходимо перенести собеседование. </p> <p>
<input type="checkbox" name="applicants[]" id="applicants_" value="1001" checked="checked" /> A <br>
<input type="checkbox" name="applicants[]" id="applicants_" value="1002" checked="checked" /> B <br>
<input type="checkbox" name="applicants[]" id="applicants_" value="1003" /> C <br>
<input type="hidden" name="confirmed_limit_message" id="confirmed_limit_message" value="1" autocomplete="off" />
</p> <a class="button secondary" href="/ru-kz/niv/groups/1">Закрыть</a>
<input type="submit" name="commit" value="Продолжить" class="button primary" data-disable-with="Продолжить" />
</form></main></body></html>"""


def booking_form_page(commit: str) -> str:
    return f"""<html><body><main id='main'>
<form id="appointment-form" novalidate="novalidate" class="formtastic appointments" action="{APPT_PATH}"
 accept-charset="UTF-8" method="post"><input type="hidden" name="authenticity_token" value="TOKEN123" autocomplete="off" />
<input type="hidden" name="confirmed_limit_message" id="confirmed_limit_message" value="1" autocomplete="off" />
<input type="hidden" name="use_consulate_appointment_capacity" id="use_consulate_appointment_capacity" value="true" autocomplete="off" />
<select name="appointments[consulate_appointment][facility_id]" id="appointments_consulate_appointment_facility_id">
<option value="135">Almaty</option> <option selected="selected" value="134">Astana</option></select>
<input icon="calendar.gif" placeholder="Date" id="appointments_consulate_appointment_date" readonly="readonly"
 class="required" type="text" name="appointments[consulate_appointment][date]" />
<select name="appointments[consulate_appointment][time]" id="appointments_consulate_appointment_time"></select>
<input type="submit" name="commit" value="{commit}" id="appointments_submit" disabled="disabled" />
</form></main></body></html>"""


def groups_page(appt_text: str | None) -> str:
    if appt_text is None:
        card = "<div class='card'><h4> Текущий статус <br> Зарегистрировать запись </h4></div>"
    else:
        card = (f"<div class='card'> <p class='consular-appt'> <strong>Консульское собеседование"
                f"<span>&#58;</span></strong> {appt_text} Astana Местное время at Astana &mdash; "
                f"<a href=\"/ru-kz/niv/schedule/72928759/addresses/consulate\">получить инструкции </a></p> </div>")
    return f"<html><body><main id='main'>{card}</main></body></html>"


# --- Разбор страниц -------------------------------------------------------------

def test_parse_limit_remaining_reads_attempts_left():
    assert monitor.parse_limit_remaining(limit_warning_page(3)) == 3
    assert monitor.parse_limit_remaining(limit_warning_page(1)) == 1


def test_parse_limit_remaining_none_without_warning():
    assert monitor.parse_limit_remaining(booking_form_page("Записаться")) is None


def test_parse_current_appointment_returns_iso_date():
    assert monitor.parse_current_appointment(groups_page("14 октябрь, 2027, 09:30")) == "2027-10-14"


def test_parse_current_appointment_accepts_genitive_month():
    assert monitor.parse_current_appointment(groups_page("3 мая, 2027, 10:00")) == "2027-05-03"


def test_parse_current_appointment_none_without_appointment():
    assert monitor.parse_current_appointment(groups_page(None)) is None


# --- Мини-сайт на MockTransport -------------------------------------------------

class FakeSite:
    """Отдаёт страницы как живой сайт и записывает каждый запрос.

    gate=True — запись уже есть: /appointment начинается с предупреждения о лимите,
    затем выбор заявителей, затем форма «Перезаписаться». gate=False — записи нет,
    форма «Записаться» лежит сразу на /appointment.
    """

    def __init__(self, left=3, gate=True, appointment="14 октябрь, 2027, 09:30", first_page=None):
        self.left, self.gate, self.appointment, self.first_page = left, gate, appointment, first_page
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = parse_qsl(request.url.query.decode())
        body = parse_qsl(request.content.decode()) if request.method == "POST" else []
        self.requests.append((request.method, request.url.path, params, body))
        path, keys = request.url.path, {k for k, _ in params}
        if path == "/ru-kz/niv/account":
            return httpx.Response(200, text=groups_page(self.appointment))
        if path != APPT_PATH:
            return httpx.Response(404)
        if request.method == "POST":
            if self.gate:
                self.left -= 1  # каждый перенос съедает попытку, как на живом сайте
            return httpx.Response(302, headers={"location": f"https://ais.usvisa-info.com{APPT_PATH}/instructions"})
        if not self.gate:
            return httpx.Response(200, text=booking_form_page("Записаться"))
        if "applicants[]" in keys:
            return httpx.Response(200, text=booking_form_page("Перезаписаться"))
        if "confirmed_limit_message" in keys:
            return httpx.Response(200, text=APPLICANTS_PAGE)
        return httpx.Response(200, text=self.first_page or limit_warning_page(self.left))

    def run(self, fn):
        async def go():
            transport = httpx.MockTransport(self.handler)
            async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
                return await fn(client)
        return asyncio.run(go())

    def posts(self):
        return [r for r in self.requests if r[0] == "POST" and r[1] == APPT_PATH]


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(monitor, "_form_cache", None)
    monkeypatch.setattr(monitor, "_appointment_date", None, raising=False)


# --- Форма брони за предупреждением о лимите ------------------------------------

def test_fetch_booking_form_walks_limit_warning_and_applicants():
    site = FakeSite(left=3)
    action, fields, left = site.run(lambda c: monitor.fetch_booking_form(c, {}))
    assert action == f"https://ais.usvisa-info.com{APPT_PATH}"
    assert fields["commit"] == "Перезаписаться"
    assert fields["authenticity_token"] == "TOKEN123"
    assert left == 3
    gets = [params for method, _, params, _ in site.requests if method == "GET"]
    assert ("confirmed_limit_message", "1") in gets[1]
    assert [v for k, v in gets[2] if k == "applicants[]"] == ["1001", "1002", "1003"]


def test_fetch_booking_form_without_appointment_has_no_limit():
    site = FakeSite(gate=False)
    _, fields, left = site.run(lambda c: monitor.fetch_booking_form(c, {}))
    assert fields["commit"] == "Записаться"
    assert left is None
    assert len(site.requests) == 1


def test_fetch_booking_form_refuses_gate_without_counter():
    site = FakeSite(first_page=APPLICANTS_PAGE)  # промежуточная страница есть, счётчика нет
    with pytest.raises(RuntimeError, match="попыт"):
        site.run(lambda c: monitor.fetch_booking_form(c, {}))


# --- Правила переноса -----------------------------------------------------------

def test_refusal_when_reserve_reached():
    assert monitor.reschedule_refusal("2027-05-10", remaining=2, appointment_date="2027-10-14")


def test_refusal_when_date_not_earlier_than_appointment():
    assert monitor.reschedule_refusal("2027-10-14", remaining=3, appointment_date="2027-10-14")
    assert monitor.reschedule_refusal("2027-10-20", remaining=3, appointment_date="2027-10-14")


def test_refusal_when_appointment_unknown():
    assert monitor.reschedule_refusal("2027-05-10", remaining=3, appointment_date=None)


def test_no_refusal_for_first_booking():
    assert monitor.reschedule_refusal("2027-05-10", remaining=None, appointment_date=None) == ""


def test_no_refusal_for_earlier_date_with_spare_attempts():
    assert monitor.reschedule_refusal("2027-05-10", remaining=3, appointment_date="2027-10-14") == ""


def test_reschedules_exhausted_at_reserve():
    assert monitor.reschedules_exhausted(2)
    assert not monitor.reschedules_exhausted(3)
    assert not monitor.reschedules_exhausted(None)  # записи нет — это первая бронь


def test_before_appointment_keeps_only_earlier_dates():
    dates = {"2027-05-10", "2027-10-14", "2027-10-20"}
    assert monitor.before_appointment(dates, "2027-10-14") == {"2027-05-10"}
    assert monitor.before_appointment(dates, None) == dates


# --- Бронь и перенос на мини-сайте ----------------------------------------------

class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)


def test_date_without_time_is_informational_and_uses_cached_empty_result():
    bot = FakeBot()
    asyncio.run(monitor.notify_new_dates(
        None, {}, bot, {"2026-11-10"}, {"2026-11-10": []},
    ))

    assert len(bot.messages) == 1
    assert "2026-11-10" in bot.messages[0]
    assert "время пока не подтверждено" in bot.messages[0]
    assert "СЛОТЫ ПОЯВИЛИСЬ" not in bot.messages[0]
    assert "Действуйте быстро" not in bot.messages[0]


def test_confirmed_time_gets_separate_urgent_notice():
    bot = FakeBot()
    asyncio.run(monitor.notify_new_dates(
        None, {}, bot, {"2026-11-10", "2026-11-11"},
        {"2026-11-10": [], "2026-11-11": ["09:30"]},
    ))

    assert len(bot.messages) == 2
    urgent, informational = bot.messages
    assert "СЛОТЫ ПОЯВИЛИСЬ" in urgent
    assert "2026-11-11" in urgent and "09:30" in urgent
    assert "2026-11-10" not in urgent
    assert "2026-11-10" in informational
    assert "2026-11-11" not in informational


def test_main_reports_unconfirmed_date_without_sms(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(monitor, "Bot", lambda token: bot)
    monkeypatch.setattr(monitor, "AUTOBOOK_ENABLED", False)
    monkeypatch.setattr(monitor, "WORKER_PROXIES", ["direct"])
    monkeypatch.setattr(monitor, "load_cookies", lambda: {"session": "test"})

    async def available_days(client, cookies, bot, max_date=""):
        return ["2026-11-10"], cookies

    async def empty_times(client, cookies, date, retries=0):
        return []

    def forbidden_sms(dates):
        raise AssertionError("SMS must not be sent")

    class StopAfterFirstCheck(Exception):
        pass

    async def stop_loop(seconds):
        raise StopAfterFirstCheck()

    monkeypatch.setattr(monitor, "fetch_available_days", available_days)
    monkeypatch.setattr(monitor, "fetch_times_for_date", empty_times)
    monkeypatch.setattr(monitor, "send_twilio_sms", forbidden_sms, raising=False)
    monkeypatch.setattr(monitor.asyncio, "sleep", stop_loop)

    with pytest.raises(StopAfterFirstCheck):
        asyncio.run(monitor.main())

    assert any("время пока не подтверждено" in message for message in bot.messages)
    assert not any("СЛОТЫ ПОЯВИЛИСЬ" in message for message in bot.messages)


def test_pending_autobook_date_is_rechecked_and_booked_when_time_appears(monkeypatch):
    date = "2027-02-10"
    bot = FakeBot()
    day_checks, time_checks, bookings = [], [], []
    monkeypatch.setattr(monitor, "Bot", lambda token: bot)
    monkeypatch.setattr(monitor, "WORKER_PROXIES", ["direct"])
    monkeypatch.setattr(monitor, "AUTOBOOK_ENABLED", True)
    monkeypatch.setattr(monitor, "AUTOBOOK_DRY_RUN", False)
    monkeypatch.setattr(monitor, "AUTOBOOK_RANGES", "2027-01-01:2027-05-31")
    monkeypatch.setattr(monitor, "_appointment_date", "2027-10-14")
    monkeypatch.setattr(monitor, "load_cookies", lambda: {"session": "test"})
    monkeypatch.setattr(monitor, "get_cached_form", lambda cookies: ("", {}, 3))

    class StopAfterSecondCheck(Exception):
        pass

    async def available_days(client, cookies, bot, max_date=""):
        day_checks.append(True)
        if len(day_checks) == 3:
            raise StopAfterSecondCheck()
        return [date], cookies

    async def available_times(client, cookies, requested_date, retries=0):
        time_checks.append(requested_date)
        return [] if len(time_checks) == 1 else ["09:30"]

    async def book(client, cookies, bot, requested_date, selected_time):
        bookings.append((requested_date, selected_time))
        return True

    async def no_wait(seconds):
        pass

    monkeypatch.setattr(monitor, "fetch_available_days", available_days)
    monkeypatch.setattr(monitor, "fetch_times_for_date", available_times)
    monkeypatch.setattr(monitor, "try_autobook", book)
    monkeypatch.setattr(monitor.asyncio, "sleep", no_wait)

    with pytest.raises(StopAfterSecondCheck):
        asyncio.run(monitor.main())

    assert time_checks == [date, date]
    assert bookings == [(date, "09:30")]


def test_reschedule_posts_earlier_date(monkeypatch):
    monkeypatch.setattr(monitor, "_appointment_date", "2027-10-14")
    site = FakeSite(left=3)
    ok, status, location = site.run(lambda c: monitor.do_real_booking(c, {}, "2027-05-10", "09:30"))
    assert ok and status == 302 and location.endswith("/appointment/instructions")
    [(_, _, _, body)] = site.posts()
    body = dict(body)
    assert body["commit"] == "Перезаписаться"
    assert body["appointments[consulate_appointment][date]"] == "2027-05-10"
    assert body["appointments[consulate_appointment][time]"] == "09:30"
    assert body["appointments[consulate_appointment][facility_id]"] == "134"


def test_reschedule_blocked_by_reserve_sends_no_post(monkeypatch):
    monkeypatch.setattr(monitor, "_appointment_date", "2027-10-14")
    site = FakeSite(left=2)
    ok, _, message = site.run(lambda c: monitor.do_real_booking(c, {}, "2027-05-10", "09:30"))
    assert not ok and "резерв" in message
    assert site.posts() == []


def test_cached_form_still_respects_reserve(monkeypatch):
    monkeypatch.setattr(monitor, "_appointment_date", "2027-10-14")
    site = FakeSite(left=2)

    async def warm_then_book(c):
        await monitor.warm_booking_form(c, {})
        return await monitor.do_real_booking(c, {}, "2027-05-10", "09:30")

    ok, _, _ = site.run(warm_then_book)
    assert not ok and site.posts() == []


def test_reschedule_to_later_date_sends_no_post(monkeypatch):
    monkeypatch.setattr(monitor, "_appointment_date", "2027-10-14")
    site = FakeSite(left=3)
    ok, _, _ = site.run(lambda c: monitor.do_real_booking(c, {}, "2027-10-20", "09:30"))
    assert not ok and site.posts() == []


def test_unknown_appointment_is_read_from_site_before_reschedule():
    site = FakeSite(left=3, appointment="14 октябрь, 2027, 09:30")
    ok, _, _ = site.run(lambda c: monitor.do_real_booking(c, {}, "2027-05-10", "09:30"))
    assert ok and len(site.posts()) == 1
    assert monitor._appointment_date == "2027-10-14"


def test_unreadable_appointment_blocks_reschedule():
    site = FakeSite(left=3, appointment=None)
    ok, _, _ = site.run(lambda c: monitor.do_real_booking(c, {}, "2027-05-10", "09:30"))
    assert not ok and site.posts() == []


def test_first_booking_needs_no_appointment():
    site = FakeSite(gate=False, appointment=None)
    ok, _, _ = site.run(lambda c: monitor.do_real_booking(c, {}, "2027-05-10", "09:30"))
    assert ok
    [(_, _, _, body)] = site.posts()
    assert dict(body)["commit"] == "Записаться"


def test_refresh_booking_state_reads_appointment_and_budget():
    site = FakeSite(left=2)
    left = site.run(lambda c: monitor.refresh_booking_state(c, {}))
    assert left == 2
    assert monitor._appointment_date == "2027-10-14"


def test_refresh_booking_state_without_appointment():
    site = FakeSite(gate=False, appointment=None)
    assert site.run(lambda c: monitor.refresh_booking_state(c, {})) is None
    assert monitor._appointment_date is None


def test_successful_reschedule_updates_appointment_and_reports(monkeypatch):
    monkeypatch.setattr(monitor, "_appointment_date", "2027-10-14")
    site = FakeSite(left=3, appointment="10 май, 2027, 09:30")  # так сайт покажет запись после POST
    bot = FakeBot()
    booked = site.run(lambda c: monitor.try_autobook(c, {}, bot, "2027-05-10", "09:30"))
    assert booked
    assert monitor._appointment_date == "2027-05-10"
    report = bot.messages[-1]
    assert "2027-10-14 → 2027-05-10" in report
    assert "подтверждено" in report
    assert "осталось 2" in report


# --- Доставка в Telegram --------------------------------------------------------

class FlakyBot:
    """Первые `failures` вызовов падают с исключением `exc`, дальше сообщения принимаются."""

    def __init__(self, failures, exc=TimedOut):
        self.failures, self.exc = failures, exc
        self.calls, self.messages = 0, []

    async def send_message(self, chat_id, text, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc("Timed out" if self.exc is TimedOut else "Can't parse entities")
        self.messages.append(text)


@pytest.fixture
def no_pauses(monkeypatch):
    monkeypatch.setattr(monitor, "TELEGRAM_RETRY_PAUSES", (0, 0))


def test_send_telegram_retries_after_timeout(no_pauses):
    bot = FlakyBot(failures=1)
    assert asyncio.run(monitor.send_telegram(bot, "0 → 14")) is True
    assert bot.calls == 2 and bot.messages == ["0 → 14"]


def test_send_telegram_gives_up_after_three_attempts(no_pauses):
    bot = FlakyBot(failures=10)
    assert asyncio.run(monitor.send_telegram(bot, "0 → 14")) is False
    assert bot.calls == 3


def test_send_telegram_does_not_retry_bad_request(no_pauses):
    bot = FlakyBot(failures=10, exc=BadRequest)
    assert asyncio.run(monitor.send_telegram(bot, "<b>broken")) is False
    assert bot.calls == 1


def full_list_log_line(caplog) -> str:
    return [r.getMessage() for r in caplog.records if "полный список" in r.getMessage()][-1]


def test_report_all_dates_logs_dates_when_delivered(caplog):
    bot = FakeBot()
    with caplog.at_level(logging.INFO, logger="monitor"):
        ok = asyncio.run(monitor.report_all_dates(
            bot, ["2027-10-15", "2027-10-06"], set(), "окон стало больше: 0 → 2", grew=True))
    assert ok
    assert "2027-10-06" in bot.messages[0] and "окон стало больше: 0 → 2" in bot.messages[0]
    line = full_list_log_line(caplog)
    assert line.startswith("Отправлен полный список")
    assert "2027-10-06, 2027-10-15" in line


def test_report_all_dates_logs_lost_message_with_dates(caplog, no_pauses):
    bot = FlakyBot(failures=10)
    with caplog.at_level(logging.INFO, logger="monitor"):
        ok = asyncio.run(monitor.report_all_dates(
            bot, ["2027-10-15", "2027-10-06"], set(), "окон стало больше: 0 → 2", grew=True))
    assert not ok
    line = full_list_log_line(caplog)
    assert line.startswith("НЕ доставлен полный список")
    assert "2027-10-06, 2027-10-15" in line


def test_report_all_dates_header_uses_astana_time(monkeypatch):
    class VpsClock(datetime):
        """Часы VPS в Бишкеке (UTC+6): 14:25 по ним — это 13:25 в Астане."""

        @classmethod
        def now(cls, tz=None):
            moment = datetime(2026, 9, 11, 8, 25, tzinfo=timezone.utc)
            if tz:
                return moment.astimezone(tz)
            return moment.astimezone(timezone(timedelta(hours=6))).replace(tzinfo=None)

    monkeypatch.setattr(monitor, "datetime", VpsClock)
    bot = FakeBot()
    asyncio.run(monitor.report_all_dates(bot, ["2027-10-06"], set(), "окон стало больше: 0 → 1", grew=True))
    assert "(11.09.2026 13:25)" in bot.messages[0]


# --- Отказ соединения: ход следующему каналу ------------------------------------

TWO_CHANNELS = ["zeus=socks5://127.0.0.1:1081", "hessen=socks5://127.0.0.1:1082"]


@pytest.fixture
def fast_retries(monkeypatch):
    monkeypatch.setattr(monitor, "TRANSIENT_BACKOFF", (0, 0, 0))
    monkeypatch.setattr(monitor, "random", types.SimpleNamespace(randint=lambda a, b: 0))
    monkeypatch.setattr(monitor, "PIN_TARGET_IP", True)


def run_days(responses):
    """days.json отдаёт по очереди элементы responses: исключение или список дат.
    Возвращает (даты, сколько было запросов)."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        item = responses[min(len(calls), len(responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json=[{"date": d, "business_day": True} for d in item])

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            dates, _ = await monitor.fetch_available_days(client, {}, FakeBot(), max_date="")
            return dates

    return asyncio.run(go()), len(calls)


def socks_refusal():
    socksio = pytest.importorskip("socksio")
    return socksio.exceptions.ProtocolError("Malformed reply")


@pytest.mark.parametrize("refusal", [lambda: httpx.ConnectError("refused"), socks_refusal],
                         ids=["ConnectError", "SOCKS ProtocolError"])
def test_connect_refusal_hands_turn_to_next_channel(monkeypatch, fast_retries, refusal):
    monkeypatch.setattr(monitor, "WORKER_PROXIES", TWO_CHANNELS)
    dates, calls = run_days([refusal(), ["2027-10-06"]])
    assert dates is None
    assert calls == 1  # ни одного ретрая через тот же канал


def test_read_timeout_is_still_retried(monkeypatch, fast_retries):
    monkeypatch.setattr(monitor, "WORKER_PROXIES", TWO_CHANNELS)
    dates, calls = run_days([httpx.ReadTimeout("slow"), ["2027-10-06"]])
    assert dates == ["2027-10-06"] and calls == 2


def test_single_channel_keeps_retrying_connect_refusal(monkeypatch, fast_retries):
    monkeypatch.setattr(monitor, "WORKER_PROXIES", ["direct"])
    dates, calls = run_days([httpx.ConnectError("refused"), ["2027-10-06"]])
    assert dates == ["2027-10-06"] and calls == 2


def test_502_days_retry_switches_to_another_target_ip(monkeypatch, fast_retries):
    bad, good = "18.254.10.38", "18.254.13.15"
    calls = []

    async def target_ips():
        return [bad, good]

    async def site_response(self, request):
        calls.append(request.url.host)
        if request.url.host == bad:
            return httpx.Response(502, request=request)
        return httpx.Response(200, request=request,
                              json=[{"date": "2027-10-06", "business_day": True}])

    monkeypatch.setattr(monitor, "resolve_target", target_ips)
    monkeypatch.setattr(monitor.random, "choice", lambda ips: ips[0], raising=False)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", site_response)

    async def check():
        transport = monitor.PinnedHostTransport(label="probe")
        async with httpx.AsyncClient(transport=transport) as client:
            days, _ = await monitor.fetch_available_days(client, {}, FakeBot(), max_date="")
        return days

    assert asyncio.run(check()) == ["2027-10-06"]
    assert calls == [bad, good]


def test_502_post_is_not_retried_or_quarantined(monkeypatch):
    ip = "18.254.10.38"
    calls = []

    async def target_ips():
        return [ip]

    async def site_response(self, request):
        calls.append((request.method, request.url.host))
        return httpx.Response(502, request=request)

    monkeypatch.setattr(monitor, "resolve_target", target_ips)
    monkeypatch.setattr(monitor, "PIN_TARGET_IP", True)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", site_response)

    async def check():
        transport = monitor.PinnedHostTransport(label="probe")
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.post(monitor.BOOKING_URL, data={"date": "2027-10-06"})
        return response.status_code, transport._quarantine

    status, quarantine = asyncio.run(check())
    assert status == 502
    assert calls == [("POST", ip)]
    assert quarantine == {}


def test_times_appearing_on_fourth_check_are_used_without_waiting_for_next_cycle(monkeypatch):
    calls, waits = [], []

    def site_response(request):
        calls.append(request.url.path)
        times = ["09:30"] if len(calls) == 4 else []
        return httpx.Response(200, json={"available_times": times})

    async def record_wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr(monitor.asyncio, "sleep", record_wait)

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(site_response)) as client:
            return await monitor.fetch_times_for_date(client, {}, "2027-10-06")

    assert asyncio.run(check()) == ["09:30"]
    assert len(calls) == 4
    assert waits == [2, 2, 2]


def test_times_502_is_retried_before_discarding_suitable_date(monkeypatch):
    calls = []

    def site_response(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(502)
        return httpx.Response(200, json={"available_times": ["09:30"]})

    async def no_wait(seconds):
        pass

    monkeypatch.setattr(monitor.asyncio, "sleep", no_wait)

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(site_response)) as client:
            return await monitor.fetch_times_for_date(client, {}, "2027-10-06", retries=1)

    assert asyncio.run(check()) == ["09:30"]
    assert len(calls) == 2


# --- Суточная сводка по каналам -------------------------------------------------

def test_parse_worker_with_and_without_name():
    assert monitor.parse_worker("zeus=socks5://127.0.0.1:1081") == ("zeus", "socks5://127.0.0.1:1081")
    assert monitor.parse_worker("socks5://127.0.0.1:1081") == ("socks5://127.0.0.1:1081", "socks5://127.0.0.1:1081")
    assert monitor.parse_worker("direct") == ("direct", "direct")


def test_channel_report_summarises_each_channel():
    stats = monitor.ChannelStats(astana(2026, 9, 10, 8))
    for ok in [True] * 9 + [False]:
        stats.note_check("zeus", ok)
    stats.note_check("hessen", True)
    for _ in range(3):
        stats.note_quarantine("zeus")
    text = stats.report(astana(2026, 9, 11, 8))
    assert "10.09 08:00 → 11.09 08:00" in text
    assert "zeus — 90.0% (провалов 1 из 10, карантинов 3)" in text
    assert "hessen — 100.0% (провалов 0 из 1, карантинов 0)" in text


def test_channel_report_due_once_a_day_at_report_hour(monkeypatch):
    monkeypatch.setattr(monitor, "CHANNEL_REPORT_HOUR", 8)
    stats = monitor.ChannelStats(astana(2026, 9, 11, 15))  # запуск после 08:00 — сегодня уже не шлём
    assert not stats.due(astana(2026, 9, 11, 23))
    assert not stats.due(astana(2026, 9, 12, 7, 59))
    assert stats.due(astana(2026, 9, 12, 8, 0))
    stats.reset(astana(2026, 9, 12, 8, 0))
    assert not stats.due(astana(2026, 9, 12, 9))


def test_channel_report_due_same_morning_after_night_start(monkeypatch):
    monkeypatch.setattr(monitor, "CHANNEL_REPORT_HOUR", 8)
    assert monitor.ChannelStats(astana(2026, 9, 12, 3)).due(astana(2026, 9, 12, 8, 1))


def test_transport_quarantine_counts_for_its_channel(monkeypatch):
    stats = monitor.ChannelStats(astana(2026, 9, 11, 8))
    monkeypatch.setattr(monitor, "_channel_stats", stats)
    transport = monitor.PinnedHostTransport(label="zeus")
    transport._mark_bad("18.254.13.15", httpx.ConnectError("refused"))
    transport._mark_bad("18.254.13.15", httpx.ConnectError("refused"))  # уже в карантине — не считается
    transport._mark_bad("18.254.10.38", httpx.ConnectError("refused"))
    stats.note_check("zeus", True)
    assert "zeus — 100.0% (провалов 0 из 1, карантинов 2)" in stats.report(astana(2026, 9, 12, 8))
