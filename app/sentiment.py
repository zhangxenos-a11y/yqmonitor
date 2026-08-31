"""情感分类：优先 DeepSeek（批量 JSON），无 Key 时退化为规则词库"""
import json
import logging
import re

import requests

from .config import config

log = logging.getLogger("yqmonitor.sentiment")

POS_WORDS = [
    "好评", "点赞", "优秀", "满意", "推荐", "感谢", "感恩", "突破", "获奖",
    "领先", "创新", "暖心", "好样的", "给力", "惊喜", "一流",
    "标杆", "楷模", "榜样", "认可", "肯定", "靠谱", "高效", "优质", "放心",
]
NEG_WORDS = [
    "投诉", "曝光", "违规", "造假", "欺诈", "骗", "坑", "差评", "恶劣", "敷衍",
    "事故", "死亡", "失踪", "失望", "退款", "维权", "举报", "抵制", "骂", "抵制",
    "糟糕", "烂", "差劲", "敷衍", "失职", "不作为", "违规", "腐败", "黑幕", "翻车",
    "质疑", "危机", "丑闻", "道歉", "处罚", "通报", "问责", "安全隐患", "质量问题",
]


def rule_classify(text: str):
    """规则词库兜底。返回 (sentiment, score, reason)。"""
    text = text or ""
    neg = sum(text.count(w) for w in NEG_WORDS)
    pos = sum(text.count(w) for w in POS_WORDS)
    if neg > pos:
        return "负面", min(0.9, 0.5 + 0.1 * neg), "词库命中负面词"
    if pos > neg:
        return "正面", min(0.9, 0.5 + 0.1 * pos), "词库命中正面词"
    return "中性", 0.4, "词库未命中倾向词"


def _extract_json(content: str):
    """从模型回复中提取 JSON 对象，容错前后杂散文字。"""
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("回复中无 JSON 对象")
    return json.loads(content[start : end + 1])


def classify_batch(items):
    """批量分类。items: [{title, snippet}] -> 返回与 items 等长的 [(sentiment, score, reason)]。"""
    if not items:
        return []
    if not config.DEEPSEEK_API_KEY:
        return [rule_classify((i.get("title") or "") + (i.get("snippet") or "")) for i in items]

    try:
        lines = []
        for idx, it in enumerate(items):
            text = f"标题：{it.get('title','')}\n摘要：{it.get('snippet','')}"
            lines.append(f"[{idx}] {text}")
        prompt = (
            "你是舆情情感分析专家。对下面每条内容判断情感倾向（正面/负面/中性），"
            "并给出 0~1 的置信度和不超过 10 字的理由。\n"
            "只输出一个 JSON 对象，格式：{\"items\":[{\"i\":序号,\"sentiment\":\"正面|负面|中性\","
            "\"score\":0.8,\"reason\":\"理由\"}]}，不要输出任何其它文字。\n\n" + "\n".join(lines)
        )
        resp = requests.post(
            f"{config.DEEPSEEK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"},
            json={
                "model": config.DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = _extract_json(content)
        by_index = {it.get("i"): it for it in parsed.get("items", [])}

        out = []
        for idx, it in enumerate(items):
            entry = by_index.get(idx)
            if entry:
                sent = entry.get("sentiment", "中性")
                if sent not in ("正面", "负面", "中性"):
                    sent = "中性"
                out.append((sent, float(entry.get("score", 0.5)), entry.get("reason", "")))
            else:
                out.append(rule_classify((it.get("title") or "") + (it.get("snippet") or "")))
        return out
    except Exception as e:  # noqa: BLE001 API 失败退化为规则词库
        log.warning("DeepSeek 情感分类失败，退化为词库: %s", e)
        return [rule_classify((i.get("title") or "") + (i.get("snippet") or "")) for i in items]
