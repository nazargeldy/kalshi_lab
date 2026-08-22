"""Kalshi API client — RSA-PSS signed requests. Read-only by design."""
import base64, os, time
from typing import Any, Dict, Optional
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

ENV = os.getenv("KALSHI_ENV", "prod").strip()
KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip()

BASE = "https://demo-api.kalshi.co" if ENV == "demo" else "https://api.elections.kalshi.com"


def load_private_key(path: str = None):
    with open(path or PRIVATE_KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


class KalshiClient:
    """Minimal signed-GET client. No order placement — read-only on purpose."""

    def __init__(self, private_key=None, base: str = BASE, key_id: str = KEY_ID):
        self.pk = private_key or load_private_key()
        self.base = base.rstrip("/")
        self.key_id = key_id
        self.s = requests.Session()

    def _headers(self, method: str, path: str) -> Dict[str, str]:
        ts = str(int(time.time() * 1000))
        msg = ts + method.upper() + path.split("?")[0]
        sig = self.pk.sign(
            msg.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    def get(self, path: str, params: Optional[dict] = None, timeout: int = 25) -> Any:
        """GET a signed endpoint. `path` starts with /trade-api/v2/..."""
        url = self.base + path
        r = self.s.get(url, headers=self._headers("GET", path), params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def try_get(self, path: str, params: Optional[dict] = None):
        """Returns (status_code, json_or_text). Never raises — for probing."""
        url = self.base + path
        try:
            r = self.s.get(url, headers=self._headers("GET", path), params=params, timeout=25)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, r.text[:300]
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"
