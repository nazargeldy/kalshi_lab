"""
Multi-channel notifier: Telegram and/or Discord.

Credentials live OUTSIDE any git repo, in ~/.kalshi_keys/:
    telegram.txt          line 1 = bot token, line 2 = chat id
                          (or JSON: {"token": "...", "chat_id": "..."})
    discord_webhook.txt   the webhook URL

Env vars override files:
    TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
    DISCORD_WEBHOOK_URL

Notes on choice of credential:
  - Telegram uses a BOT token (from @BotFather), never a user account.
  - Discord uses a WEBHOOK, never a user token. Automating a Discord user
    account is against their ToS and carries full account access.
Both are least-privilege: they can post messages and nothing else.

Any channel that is not configured is silently skipped. If none are configured,
notify() prints to stdout so the monitor keeps running regardless.
"""
import json
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


def discord_webhook():
    if "dc" in _cache:
        return _cache["dc"]
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        raw = _read_key_file("discord_webhook.txt")
        for line in (raw or "").splitlines():
            line = line.strip()
            if line.startswith("https://"):
                url = line
                break
    _cache["dc"] = url
    return url


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


def _send_discord(title, body, url=None, color=0x2ECC71):
    hook = discord_webhook()
    if not hook:
        return None
    embed = {"title": title[:250], "description": body[:3800], "color": color}
    if url:
        embed["url"] = url
    try:
        r = requests.post(hook, json={"embeds": [embed]}, timeout=12)
        if r.status_code not in (200, 204):
            print("discord HTTP {}: {}".format(r.status_code, r.text[:140]))
        return r.status_code in (200, 204)
    except Exception as e:
        print("discord error: " + str(e)[:120])
        return False


def _esc(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _md_to_html(t):
    """Convert the small subset of markdown we emit (**bold**) to Telegram HTML."""
    out, bold = [], False
    for chunk in (t or "").split("**"):
        out.append(("<b>" + _esc(chunk) + "</b>") if bold else _esc(chunk))
        bold = not bold
    return "".join(out)


def notify(title, body, url=None, color=0x2ECC71):
    """Send to every configured channel. Returns True if any succeeded."""
    results = [_send_telegram(title, body, url), _send_discord(title, body, url, color)]
    sent = [r for r in results if r is not None]
    if not sent:
        print("[no notification channel configured]\n" + title + "\n" + body
              + (("\n" + url) if url else ""))
        return False
    return any(sent)


def status():
    tok, chat = telegram_creds()
    return {
        "telegram": bool(tok and chat),
        "discord": bool(discord_webhook()),
    }


def test():
    st = status()
    print("channels configured:", st)
    if not any(st.values()):
        print("nothing to test - add credentials first")
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
