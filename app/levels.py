"""舆情级别：政府标准四级（一般 / 较大 / 重大 / 特别重大）。

级别按严重程度递增，用于人工标注、推送过滤和排序。
"""
LEVELS = ["一般", "较大", "重大", "特别重大"]

# 严重程度权重（越大越严重），用于「最低推送级别」过滤
LEVEL_WEIGHT = {name: i for i, name in enumerate(LEVELS)}

# 推送用图标
LEVEL_EMOJI = {
    "一般": "⚪",
    "较大": "🟡",
    "重大": "🟠",
    "特别重大": "🔴",
}


def level_weight(level: str) -> int:
    """返回级别的严重程度权重，未知级别按「一般」处理。"""
    return LEVEL_WEIGHT.get(level, 0)
