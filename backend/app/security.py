"""Password hashing and signed bearer tokens using only the standard library."""
import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque

from .config import BACKEND_DIR, settings

_key_cache: bytes | None = None


def _signing_key() -> bytes:
    global _key_cache
    if _key_cache is None:
        if settings.secret_key:
            _key_cache = settings.secret_key.encode()
        else:
            path = BACKEND_DIR / ".secret_key"
            if not path.exists():
                path.write_text(secrets.token_urlsafe(48))
            _key_cache = path.read_text().strip().encode()
    return _key_cache


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc, _unb64(digest))
    except Exception:
        return False


def make_token(user_id: int) -> str:
    payload = _b64(json.dumps({"sub": user_id, "exp": int(time.time()) + settings.token_ttl_hours * 3600}).encode())
    sig = _b64(hmac.new(_signing_key(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def read_token(token: str) -> int | None:
    try:
        payload, sig = token.split(".")
        expected = _b64(hmac.new(_signing_key(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(_unb64(payload))
        if data["exp"] < time.time():
            return None
        return int(data["sub"])
    except Exception:
        return None


class LoginThrottle:
    """In-memory limiter: max failures per key in a sliding window (per process; fine for a demo)."""

    def __init__(self, limit: int = 6, window_s: int = 900):
        self.limit, self.window = limit, window_s
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def _trim(self, key: str) -> deque[float]:
        q = self.hits[key]
        cutoff = time.time() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        return len(self._trim(key)) >= self.limit

    def fail(self, key: str) -> None:
        self._trim(key).append(time.time())

    def reset(self, key: str) -> None:
        self.hits.pop(key, None)


throttle = LoginThrottle()
