"""企业微信推送：智能机器人（长连接）优先，群机器人 webhook 兜底。

推送内容与行为由「设置」页配置驱动：
- 模板字段开关（标题/摘要/链接/平台/倾向/级别）
- 每批条数
- 最低推送级别（只推 >= 该级别的舆情）
- 推送模式（实时 / 定时汇总 / 两者）+ 时间窗口
"""
import logging

import requests

from .config import config
from .levels import LEVEL_EMOJI, level_weight

log = logging.getLogger("yqmonitor.push")

SENTIMENT_EMOJI = {"正面": "🟢", "负面": "🔴", "中性": "⚪"}


def _webhook_url() -> str:
    return f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={config.WECOM_WEBHOOK_KEY}"


def _send_webhook(payload: dict):
    if not config.WECOM_WEBHOOK_KEY:
        return {"ok": False, "error": "未配置企业微信机器人 Key"}
    try:
        r = requests.post(_webhook_url(), json=payload, timeout=10)
        data = r.json()
        return {"ok": data.get("errcode") == 0, "error": data.get("errmsg", "")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def push_text(content: str):
    """群机器人 webhook 发送纯文本。"""
    return _send_webhook({"msgtype": "text", "text": {"content": content}})


def _use_bot() -> bool:
    return bool(config.WECOM_BOT_ID and config.WECOM_BOT_SECRET)


def _send_by_bot(content: str):
    from . import wecom_bot

    chats = wecom_bot.bot_client.get_chats()
    if not chats:
        return {"ok": False, "error": "机器人已连接但无会话，请在企业微信里给机器人发一句话"}
    ok_all = True
    for c in chats:
        ok = wecom_bot.bot_client.send_markdown(c.get("chatid"), content)
        ok_all = ok_all and ok
    return {"ok": ok_all, "error": "" if ok_all else "发送失败"}


def _chunk_findings(findings, limit=None):
    limit = limit or int(config.PUSH_BATCH_SIZE) or 5
    for i in range(0, len(findings), limit):
        yield findings[i : i + limit]


def filter_by_level(findings):
    """按「最低推送级别」过滤。返回 (推送列表, 被过滤列表)。"""
    min_level = (config.PUSH_MIN_LEVEL or "").strip()
    if not min_level:
        return findings, []
    threshold = level_weight(min_level)
    keep, drop = [], []
    for f in findings:
        w = level_weight(f.get("level") or "一般")
        if w >= threshold:
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

        # 第一行：级别图标 + 倾向 + 平台 + 级别标签
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
            lines.append(f"{title}")
        if "snippet" in fields and snippet:
            lines.append(f"{snippet}")
        if "url" in fields and url:
            lines.append(f"{url}")
        lines.append("")
    content = "\n".join(lines)
    if len(content.encode("utf-8")) > 2000:  # 超长截断
        content = content.encode("utf-8")[:2000].decode("utf-8", "ignore")
    return content


def _send_chunks(keyword: str, findings):
    last = None
    for chunk in _chunk_findings(findings):
        content = _build_text(keyword, chunk)
        if _use_bot():
            last = _send_by_bot(content)
        else:
            last = push_text(content)
    return last or {"ok": True, "error": ""}


def push_findings(keyword: str, findings):
    """实时推送一批新发现（按级别过滤后）。返回 (result, 实际推送的条数)。"""
    if not findings:
        return {"ok": True, "error": ""}, 0
    keep, _ = filter_by_level(findings)
    if not keep:
        return {"ok": True, "error": "全部低于最低推送级别，已跳过", "skipped": len(findings)}, 0
    res = _send_chunks(keyword, keep)
    return res, len(keep)


def push_digest(items_by_keyword: dict):
    """定时汇总推送：items_by_keyword = {关键词: [findings...]}。返回统计。"""
    total = 0
    last = {"ok": True, "error": ""}
    for keyword, findings in items_by_keyword.items():
        keep, _ = filter_by_level(findings)
        if not keep:
            continue
        content = _build_text(keyword, keep)
        if _use_bot():
            last = _send_by_bot(content)
        else:
            last = push_text(content)
        total += len(keep)
    return last, total


def send_test():
    """测试推送：根据配置走智能机器人或群机器人。"""
    content = "✅ 舆情监测系统\n测试推送成功，机器人已联通。"
    if _use_bot():
        return _send_by_bot(content)
    return push_text(content)
