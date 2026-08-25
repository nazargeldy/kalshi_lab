"""
Detect the Telegram chat id after you press Start on the bot, and write it to
~/.kalshi_keys/telegram.txt. Run:  python src/setup_telegram.py
"""
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notifier import telegram_creds, _read_key_file, KEYDIR_CANDIDATES


def keyfile():
    for d in KEYDIR_CANDIDATES:
        if os.path.isdir(d):
            return os.path.join(d, "telegram.txt")
    return os.path.join(os.path.expanduser("~"), ".kalshi_keys", "telegram.txt")


def main(wait_sec=120):
    tok, _ = telegram_creds()
    if not tok:
        sys.exit("No bot token found in ~/.kalshi_keys/telegram.txt")

    me = requests.get("https://api.telegram.org/bot" + tok + "/getMe", timeout=15).json()
    if not me.get("ok"):
        sys.exit("Bot token rejected: " + str(me)[:160])
    uname = me["result"].get("username")
    print("Bot: @{}".format(uname))
    print("Open Telegram, find @{} and press Start (or send any message).".format(uname))
    print("Waiting up to {}s...".format(wait_sec))

    deadline = time.time() + wait_sec
    while time.time() < deadline:
        u = requests.get("https://api.telegram.org/bot" + tok + "/getUpdates",
                         timeout=15).json()
        for x in u.get("result", []):
            msg = x.get("message") or x.get("my_chat_member") or {}
            chat = msg.get("chat") or {}
            cid = chat.get("id")
            if cid:
                who = chat.get("username") or chat.get("first_name") or "?"
                print("Found chat: {}  ({})".format(cid, who))
                p = keyfile()
                with open(p, "w", encoding="utf-8") as f:
                    f.write(tok + "\n" + str(cid) + "\n")
                print("Saved to " + p)
                # confirm end-to-end
                r = requests.post(
                    "https://api.telegram.org/bot" + tok + "/sendMessage",
                    json={"chat_id": cid,
                          "text": "<b>Kalshi monitor connected</b>\n\n"
                                  "You'll get one message per qualifying Kalshi alert.\n\n"
                                  "<i>Paper trading only - no real funds at risk.</i>",
                          "parse_mode": "HTML"}, timeout=15).json()
                print("confirmation sent:", r.get("ok"))
                return 0
        time.sleep(3)
    print("No message received. Press Start on the bot and run this again.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
