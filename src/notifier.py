"""
Discord webhook notifier.

Deliberately uses a WEBHOOK, not a Discord account/user token:
  - a webhook can only POST messages to one channel
  - it cannot read messages, DMs, or anything else about the account
  - automating a *user* account is against Discord's ToS and risks a ban
  - if the URL leaks, the blast radius is "someone can post in that channel",
    which you fix by deleting the webhook

Reads the URL from (in priority order):
  1. DISCORD_WEBHOOK_URL env var
  2. C:/Users/geldy/.kalshi_keys/discord_webhook.txt  (or ~/.kalshi_keys/... on Linux)

If no webhook is configured, notify() degrades gracefully to stdout so the
monitor keeps running.
"""
import os

import requests

_WEBHOOK_PATHS = [
    os.path.join(os.path.expanduser("~"), ".kalshi_keys", "discord_webhook.txt"),
    r"C:/Users/geldy/.kalshi_keys/discord_webhook.txt",
]

_cached = None


def webhook_url():
    """Resolve the webhook URL once, from env or key file."""
    global _cached
    if _cached is not None:
        return _cached
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        for p in _WEBHOOK_PATHS:
            try:
                if os.path.exists(p):
                    with open(p, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("https://"):
                                url = line
                                break
                if url:
                    break
            except Exception:
                continue
    _cached = url or ""
    return _cached


def notify(title, body, url=None, color=0x2ECC71):
    """Post an alert to Discord. Returns True on success.

    Never raises - a notification failure must not take down the monitor.
    """
    hook = webhook_url()
    plain = title + "\n" + body + (("\n" + url) if url else "")
    if not hook:
        print("[no discord webhook configured]\n" + plain)
        return False

    embed = {
        "title": title[:250],
        "description": body[:3800],
        "color": color,
    }
    if url:
        embed["url"] = url
    try:
        r = requests.post(hook, json={"embeds": [embed]}, timeout=12)
        if r.status_code in (200, 204):
            return True
        print("discord HTTP {}: {}".format(r.status_code, r.text[:160]))
        return False
    except Exception as e:
        print("discord error: " + str(e)[:120])
        return False


def test():
    ok = notify(
        "Kalshi monitor connected",
        "Discord notifications are working.\n"
        "You will get one message per qualifying Kalshi alert.\n\n"
        "*Paper trading only - no real funds at risk.*",
        color=0x58A6FF)
    print("test notification sent" if ok else "test failed (see above)")
    return ok


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        test()
    else:
        print("webhook configured:", bool(webhook_url()))
