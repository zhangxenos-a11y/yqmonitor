"""配置加载：优先环境变量，其次项目根目录 .env 文件"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    """极简 .env 解析，不引入额外依赖。已存在的环境变量优先。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env(BASE_DIR / ".env")


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "yqmonitor-dev-secret-change-me")
    DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "instance" / "yqmonitor.db"))

    # DeepSeek 情感分析
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    # 企业微信机器人
    WECOM_WEBHOOK_KEY = os.getenv("WECOM_WEBHOOK_KEY", "")

    # 企业微信智能机器人（Bot ID + Secret，WebSocket 长连接）
    WECOM_BOT_ID = os.getenv("WECOM_BOT_ID", "")
    WECOM_BOT_SECRET = os.getenv("WECOM_BOT_SECRET", "")

    MONITOR_INTERVAL_MINUTES = int(os.getenv("MONITOR_INTERVAL_MINUTES", "30"))
    SEARCH_SOURCES = [
        s.strip()
        for s in os.getenv("SEARCH_SOURCES", "bing,baidu,weixin,weibo,toutiao").split(",")
        if s.strip()
    ]
    REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))

    # 扫描时间窗口（1.2.1）：只在窗口内执行抓取扫描，窗口外跳过，空=全天扫描
    SCAN_WINDOW_START = os.getenv("SCAN_WINDOW_START", "")
    SCAN_WINDOW_END = os.getenv("SCAN_WINDOW_END", "")

    # 推送配置（运行时可被「设置」页覆盖）
    PUSH_MODE = os.getenv("PUSH_MODE", "realtime")  # realtime / scheduled / both
    PUSH_TIME = os.getenv("PUSH_TIME", "09:00")     # 定时汇总的每日推送时间 HH:MM
    PUSH_WINDOW_START = os.getenv("PUSH_WINDOW_START", "")  # 实时推送时间窗口起 HH:MM，空=不限
    PUSH_WINDOW_END = os.getenv("PUSH_WINDOW_END", "")      # 实时推送时间窗口止 HH:MM，空=不限
    PUSH_FIELDS = [
        s.strip()
        for s in os.getenv("PUSH_FIELDS", "title,snippet,url,platform,sentiment,level").split(",")
        if s.strip()
    ]
    PUSH_BATCH_SIZE = int(os.getenv("PUSH_BATCH_SIZE", "5"))
    PUSH_MIN_LEVEL = os.getenv("PUSH_MIN_LEVEL", "")  # 空=全部推送；否则只推 >= 该级别

    DEFAULT_ADMIN = os.getenv("DEFAULT_ADMIN", "admin")
    DEFAULT_PASSWORD = os.getenv("DEFAULT_PASSWORD", "admin123")


config = Config()
