"""搜索引擎聚合：按关键词抓取公开搜索结果，附带链接与平台识别。

说明：抖音/微信/小红书无公开 API，本模块通过搜索引擎收录内容间接覆盖，
并依据 URL 域名识别内容所属平台。各引擎均为「尽力而为」——某源失败不影响其它源。
"""
import json
import logging
import re
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .config import config
from .db import execute, query_one

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

# 平台识别规则：域名关键词 -> 平台名（命中即返回，按顺序优先匹配）
PLATFORM_RULES = [
    # 社交 / 短视频
    ("xiaohongshu.com", "小红书"),
    ("xhslink.com", "小红书"),
    ("douyin.com", "抖音"),
    ("iesdouyin.com", "抖音"),
    ("kuaishou.com", "快手"),
    ("gifshow.com", "快手"),
    ("toutiao.com", "今日头条"),
    ("mp.weixin.qq.com", "微信公众号"),
    ("weixin.sogou.com", "微信公众号"),
    ("weibo.com", "微博"),
    ("weibo.cn", "微博"),
    ("bilibili.com", "哔哩哔哩"),
    ("b23.tv", "哔哩哔哩"),
    ("douban.com", "豆瓣"),
    ("zhihu.com", "知乎"),
    ("tieba.baidu.com", "百度贴吧"),
    ("baijiahao.baidu.com", "百家号"),
    ("tianya.cn", "天涯社区"),
    # 视频 / 直播
    ("youku.com", "优酷"),
    ("iqiyi.com", "爱奇艺"),
    ("huya.com", "虎牙直播"),
    ("douyu.com", "斗鱼直播"),
    # 资讯 / 门户
    ("sina.com", "新浪"),
    ("sohu.com", "搜狐"),
    ("163.com", "网易"),
    ("qq.com", "腾讯"),
    ("ifeng.com", "凤凰网"),
    ("thepaper.cn", "澎湃新闻"),
    ("jiemian.com", "界面新闻"),
    ("chinanews.com", "中国新闻网"),
    ("huanqiu.com", "环球网"),
    ("ce.cn", "中国经济网"),
    ("yicai.com", "第一财经"),
    ("caixin.com", "财新网"),
    ("36kr.com", "36氪"),
    ("huxiu.com", "虎嗅"),
    ("tmtpost.com", "钛媒体"),
    # 政务 / 官方
    ("gov.cn", "政府网站"),
    ("people.com.cn", "人民网"),
    ("xinhuanet.com", "新华网"),
    ("cctv.com", "央视网"),
    ("cntv.cn", "央视网"),
    ("china.com.cn", "中国网"),
    ("edu.cn", "教育网站"),
    # 百科 / 知识
    ("baike.baidu.com", "百度百科"),
    ("baike.sogou.com", "搜狗百科"),
    ("wikipedia.org", "维基百科"),
    # 技术社区
    ("csdn.net", "CSDN"),
    ("juejin.cn", "掘金"),
    ("jianshu.com", "简书"),
    ("cnblogs.com", "博客园"),
    ("segmentfault.com", "思否"),
    ("github.com", "GitHub"),
    ("gitee.com", "Gitee"),
    # 电商 / 本地生活
    ("taobao.com", "淘宝"),
    ("tmall.com", "天猫"),
    ("jd.com", "京东"),
    ("pinduoduo.com", "拼多多"),
    ("meituan.com", "美团"),
    ("dianping.com", "大众点评"),
]

# 二级/多级公共后缀：提取根域名时需保留三段
TWO_PART_TLD = {
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "com.hk", "com.tw", "co.jp", "co.uk",
}

# 运行中自动发现的新平台（域名 -> 平台名），持久化到 settings 表 custom_platforms
_custom_platforms: dict = {}
_custom_loaded = False


def _ensure_custom_loaded() -> None:
    """从数据库加载运行时已发现的自定义平台（仅一次）。"""
    global _custom_loaded
    if _custom_loaded:
        return
    _custom_loaded = True
    try:
        row = query_one("SELECT value FROM settings WHERE key='custom_platforms'")
        if row and row["value"]:
            _custom_platforms.update(json.loads(row["value"]))
    except Exception:  # noqa: BLE001 库未就绪/解析失败则忽略
        pass


def _register_platform(domain: str, label: str) -> None:
    """把新探测到的域名登记为监控源（内存 + 落库，便于重启后复用）。"""
    if not domain or domain in _custom_platforms:
        return
    _custom_platforms[domain] = label
    log.info("探测到新平台，自动加入监控源：%s → %s", domain, label)
    try:
        execute(
            "INSERT INTO settings(key,value) VALUES('custom_platforms',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(_custom_platforms, ensure_ascii=False),),
        )
    except Exception:  # noqa: BLE001
        pass


def _hostname(url: str) -> str:
    try:
        u = url if "://" in url else "http://" + url
        return urlparse(u).hostname or ""
    except Exception:  # noqa: BLE001
        return ""


def _root_domain(host: str) -> str:
    """从主机名提取根域名（去 www，保留 com.cn 等两段式后缀）。"""
    host = (host or "").lower().strip(".")
    host = re.sub(r"^www\.", "", host)
    if not host or "." not in host:
        return host
    if host.replace(".", "").isdigit():  # 纯 IP，不当作平台域名
        return ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    tail = ".".join(parts[-2:])
    if tail in TWO_PART_TLD and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def detect_platform(url: str) -> str:
    """识别 URL 所属平台；遇到未知域名时自动登记为新监控源。"""
    lowered = (url or "").lower()
    # 1. 内置规则
    for key, label in PLATFORM_RULES:
        if key in lowered:
            return label
    # 2. 运行时已发现的自定义平台
    _ensure_custom_loaded()
    for domain, label in _custom_platforms.items():
        if domain and domain in lowered:
            return label
    # 3. 探测到新平台：提取根域名自动登记
    root = _root_domain(_hostname(lowered))
    if root:
        _register_platform(root, root)
        return root
    return "网页"


def known_platforms() -> list:
    """返回全部已知平台名（内置 + 运行时发现），供前端「监控源」列表使用。"""
    _ensure_custom_loaded()
    names = [label for _, label in PLATFORM_RULES]
    names += [label for label in _custom_platforms.values()]
    # 去重并保持顺序
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


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
