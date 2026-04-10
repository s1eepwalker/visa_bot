# 🇺🇸 Монитор слотов на визу США — Посольство в Астане

Бот автоматически проверяет наличие свободных дат на `ais.usvisa-info.com`
и мгновенно присылает уведомление в Telegram, когда слот появляется.

---

## ⚡️ Быстрый старт

### 1. Установите Python 3.10+
Скачайте с [python.org](https://www.python.org/downloads/)

### 2. Установите зависимости
```bash
pip install -r requirements.txt
```

### 3. Создайте Telegram-бота
1. Откройте Telegram, найдите **@BotFather**
2. Отправьте `/newbot`, придумайте имя
3. Скопируйте выданный **токен** (вида `1234567890:ABC...`)

### 4. Узнайте ваш Chat ID
1. Найдите в Telegram **@userinfobot**
2. Напишите ему `/start`
3. Он пришлёт ваш **ID** (число)

### 5. Получите cookies с сайта посольства

> Cookies нужны, чтобы бот "представлялся" как вы — иначе сайт не даст данные.

**В Chrome/Edge:**
1. Зайдите на `https://ais.usvisa-info.com/ru-kz/niv` и **залогиньтесь**
2. Нажмите F12 → вкладка **Application** → слева **Cookies** → выберите сайт
3. Нужны куки: `_yatri_session` и `YatriSession` (и любые другие что есть)
4. Создайте файл `cookies.json` рядом с `monitor.py`:

```json
{
  "_yatri_session": "значение_отсюда",
  "YatriSession": "значение_отсюда"
}
```

**Альтернатива — расширение для браузера:**
Установите [EditThisCookie](https://chrome.google.com/webstore/detail/editthiscookie) →
зайдите на сайт → нажмите иконку → Export → скопируйте JSON

### 6. Найдите ваш Schedule ID
1. Залогиньтесь на сайте посольства
2. Откройте вашу запись на собеседование
3. В адресной строке будет URL вида:
   ```
   https://ais.usvisa-info.com/ru-kz/niv/schedule/1234567/appointment
   ```
4. Число `1234567` — это ваш **Schedule ID**

### 7. Настройте конфиг
```bash
cp .env.example .env
```
Откройте `.env` и заполните все поля.

### 8. Запустите бота
```bash
python monitor.py
```

Бот пришлёт сообщение в Telegram что запущен. Готово! ✅

---

## 📱 Как выглядит уведомление

Когда появится слот, вы получите:

```
🚨 СЛОТЫ ПОЯВИЛИСЬ!

  📅 2026-06-15: 08:00, 08:30, 09:00, 09:30, 10:00
  📅 2026-06-16: 11:00, 14:30

🔗 Забронировать сейчас →

⚡️ Действуйте быстро — слоты разбирают за минуты!
```

---

## 🖥️ Запуск 24/7 (чтобы бот работал когда компьютер выключен)

### Вариант А — VPS (рекомендуется)
Арендуйте дешёвый VPS (от $3/мес) на [DigitalOcean](https://digitalocean.com),
[Hetzner](https://hetzner.com) или [TimeWeb](https://timeweb.cloud).

```bash
# На сервере:
nohup python monitor.py &> monitor.log &
```

### Вариант Б — Windows, оставить компьютер включённым
Просто запустите `python monitor.py` в терминале и не закрывайте окно.

### Вариант В — systemd (Linux)
```ini
# /etc/systemd/system/visa-monitor.service
[Unit]
Description=US Visa Slot Monitor
After=network.target

[Service]
WorkingDirectory=/path/to/visa_monitor
ExecStart=/usr/bin/python3 monitor.py
Restart=always
EnvironmentFile=/path/to/visa_monitor/.env

[Install]
WantedBy=multi-user.target
```
```bash
systemctl enable --now visa-monitor
```

---

## ⚠️ Важные советы

- **Не ставьте интервал меньше 60 секунд** — можно получить временный бан IP
- **Cookies живут ~24 часа** — если бот получил ошибку 401, нужно обновить cookies
- **Слоты разбирают за 1-3 минуты** — держите телефон рядом когда ждёте
- Бот каждые 100 проверок присылает подтверждение что он жив

---

## 🔧 Обновление cookies

Когда cookies протухнут (ошибка 401 в логах):
1. Зайдите на сайт, залогиньтесь заново
2. Скопируйте свежие cookies
3. Обновите `cookies.json`
4. Перезапустите бот

---

## 📋 Структура файлов

```
visa_monitor/
├── monitor.py        ← основной скрипт
├── requirements.txt  ← зависимости
├── .env.example      ← шаблон конфига
├── .env              ← ваш конфиг (создать самому)
├── cookies.json      ← ваши cookies (создать самому)
└── monitor.log       ← лог работы бота (создаётся автоматически)
```
