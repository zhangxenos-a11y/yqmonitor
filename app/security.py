"""密码哈希与会话签名（仅用标准库，零额外依赖）"""
import base64
import hashlib
import hmac
import json
import os
import time

from .config import config

_ITERATIONS = 120_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return base64.b64encode(salt + dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        raw = base64.b64decode(stored.encode())
        salt, dk = raw[:16], raw[16:]
        dk2 = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
        return hmac.compare_digest(dk, dk2)
    except Exception:
        return False


def _sign(data: str) -> str:
    return hmac.new(config.SECRET_KEY.encode(), data.encode(), hashlib.sha256).hexdigest()


def make_session(user_id: int) -> str:
    """生成带过期时间的 HMAC 签名会话令牌。"""
    payload = json.dumps({"uid": user_id, "exp": int(time.time()) + 7 * 24 * 3600})
    encoded = base64.urlsafe_b64encode(payload.encode()).decode()
    return f"{encoded}.{_sign(payload)}"


def read_session(token: str):
    """校验会话令牌，返回 user_id 或 None。"""
    try:
        encoded, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded.encode()).decode()
        if not hmac.compare_digest(_sign(payload), sig):
            return None
        data = json.loads(payload)
        if data.get("exp", 0) < time.time():
            return None
        return data.get("uid")
    except Exception:
        return None
