import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BITRIX_WEBHOOK = os.getenv("BITRIX_WEBHOOK", "").strip().rstrip("/") + "/"
LEAD_SOURCE_ID = os.getenv("LEAD_SOURCE_ID", "3").strip()
LEAD_STATUS_ID = os.getenv("LEAD_STATUS_ID", "NEW").strip()
KEYWORDS = tuple(part.strip() for part in os.getenv("KEYWORDS", "").split(",") if part.strip())
MERGE_MINUTES = int(os.getenv("MERGE_MINUTES", "120"))
MERGE_SECONDS = MERGE_MINUTES * 60
MAX_MESSAGES = int(os.getenv("MAX_MESSAGES", "60"))
CONFIRM_IN_CHAT = os.getenv("CONFIRM_IN_CHAT", "false").strip().lower() in {"1", "true", "yes", "on"}
ALLOWED_GROUPS = tuple(part.strip() for part in os.getenv("ALLOWED_GROUPS", "").split(",") if part.strip())
LONG_POLLING_SECONDS = 50
HTTP_TIMEOUT = 65
MAX_TITLE = 240
MAX_COMMENTS = 60000

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
OFFSET_FILE = BASE_DIR / "offset.txt"
STATE_FILE = BASE_DIR / "lead_links.json"

FIELD_USERNAME = "UF_CRM_TELEGRAMUSERNAME_WZ"
FIELD_TELEGRAM_ID = "UF_CRM_TELEGRAMID_WZ"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("telegram-bitrix-bot")


def normalize(text: str) -> str:
    lowered = text.lower().replace("ʻ", "'").replace("ʼ", "'").replace("‘", "'")
    cleaned = re.sub(r"[^0-9a-zа-яёўқғҳ\s']", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


NORMALIZED_KEYWORDS = tuple(normalize(keyword) for keyword in KEYWORDS)
NORMALIZED_GROUPS = tuple(normalize(group) for group in ALLOWED_GROUPS)

PHONE_PATTERNS = (
    re.compile(r"\+?998[\s\-.]?\(?\d{2,3}\)?[\s\-.]?\d{3}[\s\-.]?\d{2}[\s\-.]?\d{2}"),
    re.compile(r"\+?\d{3}[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{2}[\s\-.]?\d{2}"),
    re.compile(r"\b\d{9}\b"),
)

NAME_PATTERNS = (
    re.compile(r"\bism(?:i|im)?\s*[:\-]?\s*([A-Za-z][A-Za-z'ʼ-]{1,20}(?:\s+[A-Za-z][A-Za-z'ʼ-]{1,20})?)", re.IGNORECASE),
    re.compile(r"\bname\s*[:\-]?\s*([A-Za-z][A-Za-z'-]{1,20}(?:\s+[A-Za-z][A-Za-z'-]{1,20})?)", re.IGNORECASE),
    re.compile(r"\bот\s*[:\-]?\s*([А-Яа-яЁё][А-Яа-яЁё' -]{1,20})"),
)

CONTACT_WORDS = (
    "telefon", "телефон", "phone", "contact", "контакт", "aloqa", "aloqa uchun",
    "напишите", "napishite", "yozing", "ozing", "отправьте", "jo'nating", "joningiz",
)


def find_keywords(text: str) -> list[str]:
    normalized = normalize(text)
    return [keyword for keyword, form in zip(KEYWORDS, NORMALIZED_KEYWORDS) if form and form in normalized]


def format_phone(digits: str) -> str:
    if len(digits) == 12 and digits.startswith("998"):
        digits = digits[3:]
    if len(digits) == 9 and digits.startswith("9"):
        return f"+998 {digits[0:2]} {digits[2:5]} {digits[5:7]} {digits[7:9]}"
    if len(digits) == 12 and digits.startswith("998"):
        return "+998 " + digits[3:]
    return digits


def extract_phone(text: str) -> str | None:
    if not any(word in normalize(text) for word in CONTACT_WORDS):
        for pattern in PHONE_PATTERNS:
            match = pattern.search(text)
            if match:
                return format_phone(re.sub(r"\D", "", match.group()))
        return None
    for pattern in PHONE_PATTERNS:
        match = pattern.search(text)
        if match:
            return format_phone(re.sub(r"\D", "", match.group()))
    return None


def extract_name(text: str) -> str | None:
    for pattern in NAME_PATTERNS:
        match = pattern.search(text)
        if match:
            name = match.group(1).strip(" .,!?:;-")
            name = re.split(r"\s+(?:telefon|phone|ism|name|contact|aloqa)\b", name, flags=re.IGNORECASE)[0].strip()
            if name:
                return name
    return None


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        return


def start_health_server() -> None:
    if os.getenv("ENABLE_HEALTH", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health server port=%s", port)


def telegram_api(method: str, payload: dict | None = None):
    response = requests.post(f"{TELEGRAM_API}/{method}", json=payload or {}, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram {method}: {data.get('description')}")
    return data["result"]


def bitrix_api(method: str, params: dict | None = None):
    response = requests.post(f"{BITRIX_WEBHOOK}{method}.json", json=params or {}, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(f"bitrix {method}: {data['error']} {data.get('error_description', '')}")
    return data["result"]


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, payload) -> None:
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("%s fayliga yozilmadi", path.name)


def message_link(chat: dict, message: dict) -> str:
    chat_id = str(chat.get("id", ""))
    if chat.get("type") == "supergroup" and chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message['message_id']}"
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{message['message_id']}"
    return ""


def sender_display_name(sender: dict) -> str:
    full = " ".join(part for part in (sender.get("first_name"), sender.get("last_name")) if part)
    if full:
        return full
    if sender.get("username"):
        return f"@{sender['username']}"
    return f"telegram_{sender.get('id', 'unknown')}"


def new_entry(message: dict) -> dict:
    chat = message["chat"]
    sender = message.get("from") or {}
    text = message.get("text") or message.get("caption") or ""
    return {
        "lead_id": None,
        "updated": time.time(),
        "chat_id": chat.get("id"),
        "chat_title": chat.get("title") or chat.get("type") or "",
        "user_id": sender.get("id"),
        "display_name": sender_display_name(sender),
        "username": sender.get("username") or "",
        "phone": extract_phone(text),
        "name": extract_name(text),
        "keywords": find_keywords(text),
        "messages": [
            {
                "text": text,
                "time": datetime.fromtimestamp(message.get("date", 0)).strftime("%Y-%m-%d %H:%M:%S"),
                "message_id": message.get("message_id"),
                "link": message_link(chat, message),
                "display_name": sender_display_name(sender),
                "username": sender.get("username") or "",
            }
        ],
    }


def append_message(entry: dict, message: dict) -> None:
    chat = message["chat"]
    sender = message.get("from") or {}
    text = message.get("text") or message.get("caption") or ""
    entry["messages"].append(
        {
            "text": text,
            "time": datetime.fromtimestamp(message.get("date", 0)).strftime("%Y-%m-%d %H:%M:%S"),
            "message_id": message.get("message_id"),
            "link": message_link(chat, message),
            "display_name": sender_display_name(sender),
            "username": sender.get("username") or "",
        }
    )
    entry["messages"] = entry["messages"][-MAX_MESSAGES:]
    entry["updated"] = time.time()
    entry["phone"] = entry.get("phone") or extract_phone(text)
    entry["name"] = entry.get("name") or extract_name(text)
    for keyword in find_keywords(text):
        if keyword not in entry["keywords"]:
            entry["keywords"].append(keyword)


def build_comments(entry: dict) -> str:
    lines = [
        "=== TEGMA KANAL MA'LUMOTLARI ===",
        f"Guruh: {entry['chat_title']}",
        f"Chat ID: {entry['chat_id']}",
        f"Yozuvchi: {entry['display_name']}",
    ]
    if entry.get("username"):
        lines.append(f"Telegram username: @{entry['username']}")
    lines.append(f"Telegram ID: {entry['user_id']}")
    if entry.get("phone"):
        lines.append(f"Telefon (xabardan): {entry['phone']}")
        digits = re.sub(r"\D", "", entry["phone"])
        lines.append(f"Shaxsiy chat (1 klikda javob): https://t.me/+{digits}")
    if entry.get("name"):
        lines.append(f"Ism (xabardan): {entry['name']}")
    if entry.get("keywords"):
        lines.append(f"Kalit so'zlar: {', '.join(entry['keywords'])}")
    lines += ["", f"=== XABARLAR ({len(entry['messages'])}) ===", ""]
    for index, item in enumerate(entry["messages"], 1):
        author = item["display_name"]
        if item.get("username"):
            author = f"{author} (@{item['username']})"
        lines.append(f"[{index}] {item['time']} | {author} | Xabar ID: {item.get('message_id', '-')}")
        lines.append(item["text"])
        if item.get("link"):
            lines.append(f"Havola: {item['link']}")
        lines.append("")
    return "\n".join(lines)[:MAX_COMMENTS]


def build_fields(entry: dict) -> dict:
    first_text = entry["messages"][0]["text"]
    title = f"{entry['chat_title']} | {entry['display_name']}"
    if first_text:
        title = f"{title} | {first_text}"
    fields = {
        "TITLE": title[:MAX_TITLE].strip(),
        "NAME": (entry.get("name") or entry["display_name"])[:255],
        "SOURCE_ID": LEAD_SOURCE_ID,
        "STATUS_ID": LEAD_STATUS_ID,
        "COMMENTS": build_comments(entry),
        "UTM_SOURCE": "telegram",
        "UTM_MEDIUM": "group",
        "UTM_CAMPAIGN": entry["chat_title"][:255],
        "UTM_CONTENT": str(entry["chat_id"]),
        FIELD_TELEGRAM_ID: str(entry["user_id"]),
    }
    if entry.get("username"):
        fields[FIELD_USERNAME] = f"@{entry['username']}"
    if entry.get("phone"):
        fields["PHONE"] = [{"VALUE": entry["phone"], "TYPE_ID": "WORK"}]
    if entry.get("keywords"):
        fields["UTM_TERM"] = ", ".join(entry["keywords"])[:255]
    return fields


def create_lead(entry: dict) -> int:
    result = bitrix_api(
        "crm.lead.add",
        {"fields": build_fields(entry), "params": {"REGISTER_SONAR_EVENT": "N"}},
    )
    return result if isinstance(result, int) else result["id"]


def update_lead(entry: dict) -> None:
    fields = {"COMMENTS": build_comments(entry), "UTM_TERM": ", ".join(entry["keywords"])[:255]}
    if entry.get("phone"):
        fields["PHONE"] = [{"VALUE": entry["phone"], "TYPE_ID": "WORK"}]
    if entry.get("name"):
        fields["NAME"] = entry["name"][:255]
    bitrix_api("crm.lead.update", {"id": entry["lead_id"], "fields": fields})


def load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def save_offset(offset: int) -> None:
    try:
        OFFSET_FILE.write_text(str(offset), encoding="utf-8")
    except OSError:
        logger.warning("Offset faylga yozilmadi")


lead_links: dict[str, dict] = load_json(STATE_FILE, {})


def handle_message(message: dict) -> None:
    chat = message.get("chat") or {}
    if chat.get("type") not in {"group", "supergroup"}:
        return

    chat_id = str(chat.get("id", ""))
    if ALLOWED_GROUPS and chat_id not in ALLOWED_GROUPS and normalize(str(chat.get("title", ""))) not in NORMALIZED_GROUPS:
        logger.info("Ruxsat berilmagan guruh: %s", chat.get("title"))
        return

    sender = message.get("from") or {}
    if sender.get("is_bot"):
        return

    text = message.get("text") or message.get("caption") or ""
    if not text.strip():
        return

    found = find_keywords(text)
    if NORMALIZED_KEYWORDS and not found:
        return

    key = f"{chat_id}:{sender.get('id')}"
    entry = lead_links.get(key)
    fresh = entry is not None and (time.time() - entry["updated"]) < MERGE_SECONDS and entry.get("lead_id")

    if fresh:
        append_message(entry, message)
        try:
            update_lead(entry)
        except Exception:
            logger.exception("Lead #%s yangilanmadi", entry["lead_id"])
            return
        save_json(STATE_FILE, lead_links)
        logger.info("Lead #%s ga xabar qo'shildi (%s xabar): %s", entry["lead_id"], len(entry["messages"]), text[:80])
        return

    entry = new_entry(message)
    if not found:
        entry["keywords"] = []
    try:
        entry["lead_id"] = create_lead(entry)
    except Exception:
        logger.exception("Lead yaratilmadi: %s", text[:60])
        return

    lead_links[key] = entry
    save_json(STATE_FILE, lead_links)
    logger.info(
        "Lead #%s yaratildi | guruh=%s | kim=%s | telefon=%s | %s",
        entry["lead_id"],
        entry["chat_title"],
        entry["display_name"],
        entry.get("phone") or "-",
        text[:80],
    )

    if CONFIRM_IN_CHAT:
        try:
            telegram_api(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": f"Bitrix24 da lead yaratildi: #{entry['lead_id']}",
                    "reply_to_message_id": message["message_id"],
                },
            )
        except Exception:
            logger.exception("Guruhga tasdiqlash yuborilmadi")


def run_self_test() -> None:
    sample = {
        "message_id": 1,
        "date": int(time.time()),
        "chat": {"id": -1000000000, "type": "supergroup", "title": "Sinov guruhi"},
        "from": {"id": 1, "is_bot": False, "first_name": "Sinov", "username": "sinov_user"},
        "text": "[TEST] Bu uskunaning narxi qancha? Ismi: Ali Telefon: +998 90 123 45 67",
    }
    entry = new_entry(sample)
    print(create_lead(entry))


def main() -> None:
    if not TELEGRAM_BOT_TOKEN or BITRIX_WEBHOOK == "/":
        raise SystemExit(".env da TELEGRAM_BOT_TOKEN va BITRIX_WEBHOOK to'ldirilishi kerak")

    me = telegram_api("getMe")
    logger.info("Bot ishga tushdi: @%s (%s)", me["username"], me["first_name"])
    logger.info("Kalit so'zlar: %s", ", ".join(KEYWORDS) if KEYWORDS else "barcha xabarlar")
    logger.info("Xabarlar bitta lidga jamlanadigan davomiylik: %s daqiqa", MERGE_MINUTES)
    if ALLOWED_GROUPS:
        logger.info("Ruxsat berilgan guruhlar: %s", ", ".join(ALLOWED_GROUPS))

    start_health_server()
    offset = load_offset()
    logger.info("Kutish boshlandi (offset=%s)", offset)

    while True:
        try:
            updates = telegram_api(
                "getUpdates",
                {"offset": offset, "timeout": LONG_POLLING_SECONDS, "allowed_updates": ["message"]},
            )
        except (requests.RequestException, RuntimeError) as error:
            logger.warning("Telegram ulanish xatosi: %s", error)
            time.sleep(5)
            continue
        except Exception as error:
            logger.exception("Kutilmagan xato, 10 soniyadan keyin davom: %s", error)
            time.sleep(10)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message")
            if message:
                try:
                    handle_message(message)
                except Exception:
                    logger.exception("Xabarni qayta ishlashda xato")
        if updates:
            save_offset(offset)


if __name__ == "__main__":
    if "--test-lead" in sys.argv:
        run_self_test()
    else:
        try:
            main()
        except KeyboardInterrupt:
            logger.info("Bot to'xtatildi")
