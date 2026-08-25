"""
Telegram notifier.

Credentials live OUTSIDE any git repo, in ~/.kalshi_keys/telegram.txt:
    line 1 = bot token   (from @BotFather - a BOT token, never a user account)
    line 2 = chat id
Or JSON: {"token": "...", "chat_id": "..."}
Env vars TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID override the file.

A bot token is least-privilege: it can post messages to chats that have started
the bot, and nothing else. It cannot read your account or act as you.

If Telegram is not configured, notify() prints to stdout so the monitor keeps
running regardless.
"""
import json
import re
import os

import requests

KEYDIR_CANDIDATES = [
    os.path.join(os.path.expanduser("~"), ".kalshi_keys"),
    r"C:/Users/geldy/.kalshi_keys",
]

_cache = {}


def _read_key_file(name):
    for d in KEYDIR_CANDIDATES:
        p = os.path.join(d, name)
        try:
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    return f.read().strip()
        except Exception:
            continue
    return ""


def telegram_creds():
    """Return (token, chat_id) or ('','')."""
    if "tg" in _cache:
        return _cache["tg"]
    tok = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not (tok and chat):
        raw = _read_key_file("telegram.txt")
        if raw:
            try:                                    # JSON form
                d = json.loads(raw)
                tok = tok or str(d.get("token", "")).strip()
                chat = chat or str(d.get("chat_id", "")).strip()
            except Exception:                       # two-line form
                lines = [l.strip() for l in raw.splitlines() if l.strip()]
                if len(lines) >= 1 and not tok:
                    tok = lines[0]
                if len(lines) >= 2 and not chat:
                    chat = lines[1]
    _cache["tg"] = (tok, chat)
    return _cache["tg"]


def _send_telegram(title, body, url=None):
    tok, chat = telegram_creds()
    if not (tok and chat):
        return None                                  # not configured
    text = "<b>" + _esc(title) + "</b>\n\n" + _md_to_html(body)
    if url:
        text += "\n\n<a href='" + url + "'>Open on Kalshi</a>"
    try:
        r = requests.post(
            "https://api.telegram.org/bot" + tok + "/sendMessage",
            json={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=12)
        if not r.ok:
            print("telegram HTTP {}: {}".format(r.status_code, r.text[:140]))
        return r.ok
    except Exception as e:
        print("telegram error: " + str(e)[:120])
        return False


def _esc(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _md_to_html(t):
    """Convert the markdown subset we emit (**bold**, [text](url)) to Telegram HTML.

    Links are extracted first and re-inserted after escaping, so the anchor
    markup survives HTML-escaping of the surrounding text.
    """
    t = t or ""
    links = []

    def _stash(m):
        links.append((m.group(1), m.group(2)))
        return "\x00LINK{}\x00".format(len(links) - 1)

    t = _MD_LINK.sub(_stash, t)

    out, bold = [], False
    for chunk in t.split("**"):
        out.append(("<b>" + _esc(chunk) + "</b>") if bold else _esc(chunk))
        bold = not bold
    html = "".join(out)

    for i, (text, url) in enumerate(links):
        html = html.replace("\x00LINK{}\x00".format(i),
                            "<a href='" + url + "'>" + _esc(text) + "</a>")
    return html


def notify(title, body, url=None, color=None):
    """Send the alert to Telegram. Returns True on success.

    `color` is accepted and ignored so callers don't need to care which
    channel is configured. Never raises: a notification failure must not
    take down the monitor.
    """
    r = _send_telegram(title, body, url)
    if r is None:
        print("[telegram not configured]\n" + title + "\n" + body
              + (("\n" + url) if url else ""))
        return False
    return bool(r)


def status():
    tok, chat = telegram_creds()
    return {"telegram": bool(tok and chat)}


def test():
    st = status()
    print("channels configured:", st)
    if not st["telegram"]:
        print("nothing to test - add ~/.kalshi_keys/telegram.txt first")
        return False
    ok = notify(
        "Kalshi monitor connected",
        "Notifications are working.\n"
        "You will get one message per qualifying Kalshi alert.\n\n"
        "**Paper trading only - no real funds at risk.**",
        color=0x58A6FF)
    print("test sent" if ok else "test failed (see above)")
    return ok


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        test()
    else:
        print(status())
