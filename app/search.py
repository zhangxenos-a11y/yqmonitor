"""搜索引擎聚合：按关键词抓取公开搜索结果，附带链接与平台识别。

说明：抖音/微信/小红书无公开 API，本模块通过搜索引擎收录内容间接覆盖，
并依据 URL 域名识别内容所属平台。各引擎均为「尽力而为」——某源失败不影响其它源。
"""
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

from .config import config

log = logging.getLogger("yqmonitor.search")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_session = requests.Session()
_session.headers.update(HEADERS)

# 平台识别规则：域名关键词 -> 平台名（命中即返回）
PLATFORM_RULES = [
    ("xiaohongshu.com", "小红书"),
    ("xhslink.com", "小红书"),
    ("douyin.com", "抖音"),
    ("iesdouyin.com", "抖音"),
    ("toutiao.com", "今日头条"),
    ("mp.weixin.qq.com", "微信公众号"),
    ("weixin.sogou.com", "微信公众号"),
    ("weibo.com", "微博"),
    ("weibo.cn", "微博"),
    ("bilibili.com", "哔哩哔哩"),
    ("zhihu.com", "知乎"),
    ("baijiahao.baidu.com", "百家号"),
    ("baike.baidu.com", "百度百科"),
    ("baike.sogou.com", "搜狗百科"),
    ("sohu.com", "搜狐"),
    ("163.com", "网易"),
    ("qq.com", "腾讯"),
    ("gov.cn", "政府网站"),
    ("edu.cn", "教育网站"),
]


def detect_platform(url: str) -> str:
    lowered = (url or "").lower()
    for key, label in PLATFORM_RULES:
        if key in lowered:
            return label
    return "网页"


def _clean(s) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _resolve_baidu(url: str) -> str:
    """把百度 /link?url= 跳转链接解析为真实目标 URL（尽力而为）。"""
    if "baidu.com/link" not in url:
        return url
    try:
        r = _session.get(
            url,
            headers={"Referer": "https://www.baidu.com/"},
            timeout=6,
            allow_redirects=True,
        )
        if "baidu.com/link" not in r.url and r.url.startswith("http"):
            return r.url
    except Exception:  # noqa: BLE001
        pass
    return url


def search_baidu(keyword: str, limit: int = 10):
    """百度搜索。结果项 title/url/snippet。反爬限流时返回空，由上层容错。"""
    results = []
    r = _session.get(
        "https://www.baidu.com/s",
        params={"wd": keyword, "rn": str(limit), "ie": "utf-8"},
        timeout=config.REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    if len(r.text) < 2000:  # 反爬空壳页，直接放弃
        return results
    soup = BeautifulSoup(r.text, "lxml")
    for h3 in soup.select("h3.t a, h3.c-title a")[:limit]:
        title = _clean(h3.get_text())
        href = (h3.get("href") or "").strip()
        if not title:
            continue
        # 优先取真实链接（mu 属性），否则解析跳转链接
        real = (h3.get("mu") or h3.get("data-landurl") or "").strip()
        if real.startswith("http"):
            href = real
        elif "baidu.com/link" in href:
            href = _resolve_baidu(href)
        if not href.startswith("http"):
            continue
        container = h3.find_parent("div", class_=re.compile("c-container|result"))
        snippet = ""
        if container:
            el = container.select_one(".c-abstract, .c-span-last, .content-right_8Zs40, span.content-right_8Zs40")
            snippet = _clean(el.get_text()) if el else ""
        results.append({"title": title, "url": href, "snippet": snippet, "engine": "baidu"})
    return results


def search_bing(keyword: str, limit: int = 10):
    """必应搜索（国内版），反爬较弱、较稳定。"""
    results = []
    r = _session.get(
        "https://cn.bing.com/search",
        params={"q": keyword, "count": str(limit)},
        timeout=config.REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    for li in soup.select("li.b_algo")[:limit]:
        a = li.select_one("h2 a")
        if not a:
            continue
        title = _clean(a.get_text())
        href = (a.get("href") or "").strip()
        if not title or not href.startswith("http"):
            continue
        p = li.select_one(".b_caption p, .b_lineclamp2, .b_paractl")
        snippet = _clean(p.get_text()) if p else ""
        results.append({"title": title, "url": href, "snippet": snippet, "engine": "bing"})
    return results


def search_weixin(keyword: str, limit: int = 10):
    """搜狗微信搜索（微信公众号文章）。先取首页 Cookie 再搜，规避验证码。

    说明：搜狗 /link?url= 跳转链接被搜狗自身反爬（/antispider/）拦截，
    requests 环境无法解析出真实 mp.weixin.qq.com 链接，故保留跳转链接。
    """
    results = []
    try:
        # 先访问首页拿 SUID/SNUID Cookie，否则易触发验证码
        _session.get("https://weixin.sogou.com/", timeout=config.REQUEST_TIMEOUT)
    except Exception:  # noqa: BLE001
        pass
    r = _session.get(
        "https://weixin.sogou.com/weixin",
        params={"type": "2", "query": keyword, "ie": "utf8"},
        timeout=config.REQUEST_TIMEOUT,
    )
    if "验证码" in r.text or "antispider" in r.url:
        log.warning("搜狗微信触发验证码，跳过")
        return results
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    for li in soup.select("ul.news-list li")[:limit]:
        a = li.select_one(".txt-box h3 a")
        if not a:
            continue
        title = _clean(a.get_text())
        href = a.get("href", "")
        if not title:
            continue
        if href.startswith("/"):
            href = "https://weixin.sogou.com" + href
        p = li.select_one(".txt-info")
        snippet = _clean(p.get_text()) if p else ""
        if href.startswith("http"):
            results.append({"title": title, "url": href, "snippet": snippet, "engine": "weixin"})
    return results


def search_weibo(keyword: str, limit: int = 10):
    """微博搜索。未登录返回内容有限，尽力而为。"""
    results = []
    r = _session.get(
        "https://s.weibo.com/weibo",
        params={"q": keyword},
        timeout=config.REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    for card in soup.select("div.card-wrap")[:limit]:
        a = card.select_one(".from a, .txt[action-type=feed_list_item] a[href*=weibo], h3 a")
        if not a:
            continue
        href = a.get("href", "")
        title = _clean(a.get_text())
        if not title:
            # 取正文片段作标题
            txt = card.select_one(".txt")
            title = _clean(txt.get_text())[:40] if txt else keyword
        if href.startswith("//"):
            href = "https:" + href
        if href.startswith("http"):
            results.append({"title": title, "url": href, "snippet": "", "engine": "weibo"})
    return results


def search_toutiao(keyword: str, limit: int = 10):
    """头条搜索内容接口（JSON），尽力而为。"""
    results = []
    r = _session.get(
        "https://www.toutiao.com/api/search/content/",
        params={
            "aid": "24",
            "offset": 0,
            "count": limit,
            "keyword": keyword,
            "search_source": "normal",
        },
        timeout=config.REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    items = data.get("data") or []
    if isinstance(items, dict):
        items = items.get("data") or []
    for it in items[:limit]:
        title = it.get("title") or it.get("share_title") or ""
        url = it.get("article_url") or it.get("display_url") or ""
        snippet = it.get("abstract") or it.get("summary") or ""
        if title and url:
            results.append({"title": _clean(title), "url": url, "snippet": _clean(snippet), "engine": "toutiao"})
    return results


ENGINES = {
    "baidu": {"name": "百度搜索", "func": search_baidu},
    "bing": {"name": "必应搜索", "func": search_bing},
    "weixin": {"name": "微信(搜狗)", "func": search_weixin},
    "weibo": {"name": "微博", "func": search_weibo},
    "toutiao": {"name": "头条搜索", "func": search_toutiao},
}


def search_all(keyword: str, engines=None, limit: int = 10):
    """遍历启用的引擎，返回 (results, errors)。
    results 每项含 title/url/snippet/engine/source_platform。
    """
    engines = engines or config.SEARCH_SOURCES
    results, errors = [], {}
    for name in engines:
        entry = ENGINES.get(name)
        if not entry:
            continue
        try:
            items = entry["func"](keyword, limit)
            for it in items:
                it["engine"] = name
                it["source_platform"] = detect_platform(it["url"])
            results.extend(items)
        except Exception as e:  # noqa: BLE001 单源失败不阻断整体
            errors[name] = f"{type(e).__name__}: {e}"
            log.warning("搜索源 %s 失败: %s", name, e)
        time.sleep(1)  # 引擎间限速，降低封禁概率
    return results, errors
