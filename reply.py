import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

import bot


def usage() -> None:
    print("Foydalanish:")
    print('  python reply.py <lead_id> "<xabar matni>"')
    print('  python reply.py <lead_id> --reply-to <message_id> "<xabar matni>"')
    print('  python reply.py <lead_id> --info')


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        usage()
        raise SystemExit(1)

    lead_id = int(argv[0])
    lead = bot.bitrix_api(
        "crm.lead.get",
        {"id": str(lead_id), "select": ["ID", "TITLE", "COMMENTS", "UTM_CONTENT", "UTM_CAMPAIGN", bot.FIELD_USERNAME, bot.FIELD_TELEGRAM_ID, "PHONE"]},
    )

    if "--info" in argv:
        print(f"ID: {lead['ID']}")
        print(f"Sarlavha: {lead.get('TITLE')}")
        print(f"Guruh: {lead.get('UTM_CAMPAIGN')}")
        print(f"Chat ID: {lead.get('UTM_CONTENT')}")
        print(f"Username: {lead.get(bot.FIELD_USERNAME)}")
        print(f"Telegram ID: {lead.get(bot.FIELD_TELEGRAM_ID)}")
        print(f"Telefon: {lead.get('PHONE')}")
        return

    chat_id = lead.get("UTM_CONTENT")
    if not chat_id:
        raise SystemExit("Bu lid Telegram guruhidan kelmagan (UTM_CONTENT bo'sh)")

    text = argv[-1]
    payload = {"chat_id": int(chat_id), "text": text}
    if "--reply-to" in argv:
        index = argv.index("--reply-to")
        payload["reply_to_message_id"] = int(argv[index + 1])

    sent = bot.telegram_api("sendMessage", payload)
    print(f"Yuborildi -> {chat_id}, xabar ID {sent['message_id']}")


if __name__ == "__main__":
    main(sys.argv[1:])
