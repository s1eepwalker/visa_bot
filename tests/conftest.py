import os
import sys
import tempfile
from pathlib import Path

# Изоляция от боевого .env: load_dotenv() не перезаписывает уже заданные переменные,
# поэтому реальные логин, токены и Twilio в тесты не попадут.
os.environ.update({
    "TELEGRAM_TOKEN": "test-token",
    "TELEGRAM_CHAT_ID": "0",
    "SCHEDULE_ID": "72928759",
    "EMAIL": "",
    "PASSWORD": "",
    "MAX_DATE": "",
    "AUTOBOOK_ENABLED": "true",
    "AUTOBOOK_RANGES": "",
    "AUTOBOOK_DRY_RUN": "false",
    "RESCHEDULE_RESERVE": "2",
    "WORKER_PROXIES": "direct",
    "PIN_TARGET_IP": "false",
    "TWILIO_ACCOUNT_SID": "",
    "TWILIO_AUTH_TOKEN": "",
    "TWILIO_FROM_NUMBER": "",
    "TWILIO_TO_NUMBER": "",
})

# monitor.py пишет monitor.log, cookies.json и booked.json в текущий каталог — уводим во временный.
os.chdir(tempfile.mkdtemp(prefix="visa-monitor-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
