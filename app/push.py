"""多终端推送：企业微信智能机器人 / 企业微信群机器人 / 飞书 / 钉钉 / Server酱 / PushPlus / 通用 Webhook。

推送内容与行为由「设置」页配置驱动：
- 模板字段开关（标题/摘要/链接/平台/倾向/级别）
- 每批条数
- 最低推送级别（全局，另每个终端可单独设更严格的最低级别）
- 推送模式（实时 / 定时汇总 / 两者）+ 时间窗口

终端配置存于 push_channels 表，可任意增删多个终端、按需启停。
"""
import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse

import requests

from .config import config
from .db import execute, query
from .levels import LEVEL_EMOJI, level_weight

log = logging.getLogger("yqmonitor.push")

SENTIMENT_EMOJI = {"正面": "🟢", "负面": "🔴", "中性": "⚪"}

# 终端类型 -> 中文名
CHANNEL_TYPES = {
    "wecom_bot": "企业微信智能机器人",
    "wecom_webhook": "企业微信群机器人",
    "feishu": "飞书机器人",
    "dingtalk": "钉钉机器人",
    "serverchan": "Server酱（微信）",
    "pushplus": "PushPlus（微信）",
    "webhook": "通用 Webhook",
}

# 各类型需要填写的字段（前端据此动态生成表单）
CHANNEL_FIELDS = {
    "wecom_bot": [("bot_id", "Bot ID"), ("secret", "Secret")],
    "wecom_webhook": [("key", "webhook key（URL 中 ?key= 之后）")],
    "feishu": [("url", "webhook 地址"), ("secret", "签名密钥（可选）")],
    "dingtalk": [("token", "access_token"), ("secret", "加签密钥（可选）")],
    "serverchan": [("sendkey", "SendKey")],
    "pushplus": [("token", "token")],
    "webhook": [("url", "接收地址"), ("token", "Bearer token（可选）")],
}


# ---------- 终端读取 ----------
def _migrate_legacy() -> None:
    """兼容旧版：若尚无任何终端，且 .env/设置里配了旧的企业微信，自动创建默认终端（仅一次）。"""
    row = query("SELECT id FROM push_channels LIMIT 1")
    if row:
        return
    if query("SELECT value FROM settings WHERE key='legacy_channels_migrated'"):
        return
    execute(
        "INSERT INTO settings(key,value) VALUES('legacy_channels_migrated','1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    if config.WECOM_BOT_ID and config.WECOM_BOT_SECRET:
        execute(
            "INSERT INTO push_channels(name,type,config,enabled,min_level) VALUES(?,?,?,1,'')",
            ("企业微信智能机器人", "wecom_bot",
             json.dumps({"bot_id": config.WECOM_BOT_ID, "secret": config.WECOM_BOT_SECRET}, ensure_ascii=False)),
        )
    if config.WECOM_WEBHOOK_KEY:
        execute(
            "INSERT INTO push_channels(name,type,config,enabled,min_level) VALUES(?,?,?,1,'')",
            ("企业微信群机器人", "wecom_webhook",
             json.dumps({"key": config.WECOM_WEBHOOK_KEY}, ensure_ascii=False)),
        )


def get_channels(enabled_only: bool = False):
    """读取推送终端列表。config 字段反序列化为 dict。"""
    _migrate_legacy()
    sql = "SELECT * FROM push_channels" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id ASC"
    channels = []
    for r in query(sql):
        d = dict(r)
        try:
            d["config"] = json.loads(d.get("config") or "{}")
        except Exception:  # noqa: BLE001
            d["config"] = {}
        channels.append(d)
    return channels


# ---------- 各终端发送实现 ----------
def _send_wecom_bot(channel, content, title):  # noqa: ARG001
    from . import wecom_bot

    if not (config.WECOM_BOT_ID and config.WECOM_BOT_SECRET):
        return {"ok": False, "error": "未配置智能机器人 Bot ID/Secret"}
    chats = wecom_bot.bot_client.get_chats()
    if not chats:
        return {"ok": False, "error": "机器人已连接但无会话，请在企业微信里给机器人发一句话"}
    ok_all = True
    for c in chats:
        ok = wecom_bot.bot_client.send_markdown(c.get("chatid"), content)
        ok_all = ok_all and ok
    return {"ok": ok_all, "error": "" if ok_all else "发送失败"}


def _send_wecom_webhook(channel, content, title):  # noqa: ARG001
    key = (channel.get("config") or {}).get("key")
    if not key:
        return {"ok": False, "error": "缺少群机器人 key"}
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
    try:
        r = requests.post(url, json={"msgtype": "markdown", "markdown": {"content": content}}, timeout=10)
        d = r.json()
        return {"ok": d.get("errcode") == 0, "error": d.get("errmsg", "")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _feishu_sign(secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    h = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(h).decode("utf-8")


def _send_feishu(channel, content, title):  # noqa: ARG001
    cfg = channel.get("config") or {}
    url = cfg.get("url")
    if not url:
        return {"ok": False, "error": "缺少飞书 webhook 地址"}
    secret = cfg.get("secret")
    if secret:
        ts = str(int(time.time()))
        sign = _feishu_sign(secret, ts)
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}timestamp={ts}&sign={urllib.parse.quote(sign)}"
    try:
        r = requests.post(url, json={"msg_type": "text", "content": {"text": content}}, timeout=10)
        d = r.json()
        code = d.get("code") if isinstance(d, dict) else None
        return {"ok": code == 0, "error": d.get("msg", "") if isinstance(d, dict) else ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _dingtalk_sign(secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    h = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    return urllib.parse.quote_plus(base64.b64encode(h))


def _send_dingtalk(channel, content, title):
    cfg = channel.get("config") or {}
    token = cfg.get("token")
    if not token:
        return {"ok": False, "error": "缺少钉钉 access_token"}
    url = f"https://oapi.dingtalk.com/robot/send?access_token={token}"
    secret = cfg.get("secret")
    if secret:
        ts = str(round(time.time() * 1000))
        sign = _dingtalk_sign(secret, ts)
        url += f"&timestamp={ts}&sign={sign}"
    try:
        r = requests.post(url, json={"msgtype": "markdown", "markdown": {"title": title, "text": content}}, timeout=10)
        d = r.json()
        return {"ok": d.get("errcode") == 0, "error": d.get("errmsg", "")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _send_serverchan(channel, content, title):
    cfg = channel.get("config") or {}
    key = cfg.get("sendkey")
    if not key:
        return {"ok": False, "error": "缺少 Server酱 SendKey"}
    try:
        r = requests.post(f"https://sctapi.ftqq.com/{key}.send", data={"title": title, "desp": content}, timeout=10)
        d = r.json()
        return {"ok": d.get("code") == 0, "error": d.get("message", "")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _send_pushplus(channel, content, title):
    cfg = channel.get("config") or {}
    token = cfg.get("token")
    if not token:
        return {"ok": False, "error": "缺少 PushPlus token"}
    try:
        r = requests.post(
            "https://www.pushplus.plus/send",
            json={"token": token, "title": title, "content": content, "template": "markdown"},
            timeout=10,
        )
        d = r.json()
        return {"ok": d.get("code") == 200, "error": d.get("msg", "")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _send_webhook(channel, content, title):
    cfg = channel.get("config") or {}
    url = cfg.get("url")
    if not url:
        return {"ok": False, "error": "缺少 webhook URL"}
    headers = {"Content-Type": "application/json"}
    if cfg.get("token"):
        headers["Authorization"] = "Bearer " + cfg["token"]
    try:
        r = requests.post(url, json={"title": title, "content": content, "text": content}, headers=headers, timeout=10)
        return {"ok": r.status_code < 300, "error": f"HTTP {r.status_code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


_SENDERS = {
    "wecom_bot": _send_wecom_bot,
    "wecom_webhook": _send_wecom_webhook,
    "feishu": _send_feishu,
    "dingtalk": _send_dingtalk,
    "serverchan": _send_serverchan,
    "pushplus": _send_pushplus,
    "webhook": _send_webhook,
}


def send_to_channel(channel, content: str, title: str = ""):
    """按终端类型分发发送。channel 为含 type/config 的 dict。"""
    sender = _SENDERS.get(channel.get("type"))
    if not sender:
        return {"ok": False, "error": f"未知终端类型 {channel.get('type')}"}
    return sender(channel, content, title)


# ---------- 内容构建 ----------
def _chunk_findings(findings, limit=None):
    limit = limit or int(config.PUSH_BATCH_SIZE) or 5
    for i in range(0, len(findings), limit):
        yield findings[i : i + limit]


def filter_by_min_level(findings, min_level):
    """按指定最低级别过滤（min_level 空 = 全部）。"""
    min_level = (min_level or "").strip()
    if not min_level:
        return findings
    threshold = level_weight(min_level)
    return [f for f in findings if level_weight(f.get("level") or "一般") >= threshold]


def filter_by_level(findings):
    """按全局「最低推送级别」过滤。返回 (推送列表, 被过滤列表)。"""
    min_level = (config.PUSH_MIN_LEVEL or "").strip()
    if not min_level:
        return findings, []
    threshold = level_weight(min_level)
    keep, drop = [], []
    for f in findings:
        if level_weight(f.get("level") or "一般") >= threshold:
            keep.append(f)
        else:
            drop.append(f)
    return keep, drop


def _build_text(keyword: str, chunk) -> str:
    """按模板字段开关渲染推送内容。"""
    fields = set(config.PUSH_FIELDS)
    lines = [f"🚨 舆情监测 · 「{keyword}」", f"新增 {len(chunk)} 条", ""]
    for f in chunk:
        sentiment = f.get("sentiment") or "中性"
        level = f.get("level") or "一般"
        platform = f.get("source_platform") or "网页"
        title = (f.get("title") or "").strip()[:60]
        url = (f.get("url") or "").strip()
        snippet = (f.get("snippet") or "").strip()[:80]

        head_parts = []
        if "sentiment" in fields:
            head_parts.append(f"{SENTIMENT_EMOJI.get(sentiment, '⚪')} [{sentiment}]")
        if "platform" in fields:
            head_parts.append(platform)
        if "level" in fields:
            head_parts.append(f"{LEVEL_EMOJI.get(level, '⚪')}{level}")
        if head_parts:
            lines.append(" ".join(head_parts))
        if "title" in fields and title:
            lines.append(title)
        if "snippet" in fields and snippet:
            lines.append(snippet)
        if "url" in fields and url:
            lines.append(url)
        lines.append("")
    content = "\n".join(lines)
    if len(content.encode("utf-8")) > 2000:
        content = content.encode("utf-8")[:2000].decode("utf-8", "ignore")
    return content


def _push_content_to_channel(channel, keyword: str, findings):
    """把一个关键词的发现分块发送到单个终端。返回 {'ok', 'error'}。"""
    min_level = channel.get("min_level") or ""
    keep = filter_by_min_level(findings, min_level)
    if not keep:
        return {"ok": True, "error": "", "skipped": len(findings)}
    title = f"舆情监测 · {keyword}"
    last = {"ok": True, "error": ""}
    for chunk in _chunk_findings(keep):
        content = _build_text(keyword, chunk)
        last = send_to_channel(channel, content, title)
        if not last.get("ok"):
            break
    return last


def _push_all_channels(channels, keyword, findings):
    """遍历终端推送，返回 {'ok', 'error', 'sent'}。ok=True 表示至少一个终端成功。"""
    sent_any = False
    errors = []
    for ch in channels:
        res = _push_content_to_channel(ch, keyword, findings)
        if res.get("ok"):
            sent_any = True
        else:
            errors.append(f"{ch.get('name')}: {res.get('error') or '失败'}")
    return {"ok": sent_any, "error": "；".join(errors), "sent": sent_any}


def push_findings(keyword: str, findings):
    """实时推送一批新发现（全局级别过滤后）。返回 (result, 实际推送的条数)。"""
    if not findings:
        return {"ok": True, "error": ""}, 0
    keep, _ = filter_by_level(findings)
    if not keep:
        return {"ok": True, "error": "全部低于最低推送级别，已跳过", "skipped": len(findings)}, 0
    channels = get_channels(enabled_only=True)
    if not channels:
        return {"ok": False, "error": "未配置任何推送终端"}, 0
    res = _push_all_channels(channels, keyword, keep)
    return res, len(keep)


def push_single(keyword: str, finding: dict):
    """推送单条舆情（调用方已确认级别符合推送标准），返回 (result, 实际推送条数)。"""
    channels = get_channels(enabled_only=True)
    if not channels:
        return {"ok": False, "error": "未配置任何推送终端"}, 0
    res = _push_all_channels(channels, keyword, [finding])
    return res, 1 if res.get("ok") else 0


def push_digest(items_by_keyword: dict):
    """定时汇总推送：items_by_keyword = {关键词: [findings...]}。返回 (result, 推送条数)。"""
    channels = get_channels(enabled_only=True)
    if not channels:
        return {"ok": False, "error": "未配置任何推送终端"}, 0
    total = 0
    sent_any = False
    errors = []
    for keyword, findings in items_by_keyword.items():
        keep, _ = filter_by_level(findings)
        if not keep:
            continue
        total += len(keep)
        for ch in channels:
            res = _push_content_to_channel(ch, keyword, keep)
            if res.get("ok"):
                sent_any = True
            else:
                errors.append(f"{ch.get('name')}: {res.get('error') or '失败'}")
    return {"ok": sent_any, "error": "；".join(errors)}, total


def send_test():
    """测试推送：向全部启用终端各发一条测试消息。返回 {'ok','results'}。"""
    channels = get_channels(enabled_only=True)
    if not channels:
        return {"ok": False, "error": "未配置任何推送终端"}
    content = "✅ 舆情监测系统\n测试推送成功，终端已联通。"
    results = []
    for ch in channels:
        r = send_to_channel(ch, content, "舆情监测系统测试")
        results.append({"name": ch.get("name"), "type": ch.get("type"), "ok": r.get("ok"), "error": r.get("error")})
    ok_all = all(r["ok"] for r in results)
    return {"ok": ok_all, "results": results}
