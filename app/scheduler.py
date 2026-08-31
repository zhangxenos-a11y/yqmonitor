"""定时监控调度：定期遍历关键词 -> 聚合搜索 -> 去重入库 -> 情感分类 -> 推送。

推送策略由配置驱动：
- 实时模式：监控发现新舆情立即推送（可受时间窗口限制）
- 定时汇总：每天固定时间把「尚未推送」的舆情汇总推送一次
"""
import hashlib
import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import config
from .db import execute, query
from .push import push_digest, push_findings
from .search import search_all
from .sentiment import classify_batch

log = logging.getLogger("yqmonitor.scheduler")

scheduler = BackgroundScheduler(timezone="Asia/Shanghai")


def _url_hash(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def _get_setting(key: str, default=""):
    row = query("SELECT value FROM settings WHERE key=?", (key,))
    return row[0]["value"] if row else default


def _within_window() -> bool:
    """判断当前时间是否在实时推送时间窗口内。窗口为空=不限。"""
    start = (config.PUSH_WINDOW_START or "").strip()
    end = (config.PUSH_WINDOW_END or "").strip()
    if not start and not end:
        return True
    now = datetime.now().strftime("%H:%M")
    if start and end:
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end  # 跨天窗口
    if start:
        return now >= start
    return now <= end


def _within_scan_window() -> bool:
    """判断当前时间是否在扫描时间窗口内。窗口为空=全天扫描。"""
    start = (config.SCAN_WINDOW_START or "").strip()
    end = (config.SCAN_WINDOW_END or "").strip()
    if not start and not end:
        return True
    now = datetime.now().strftime("%H:%M")
    if start and end:
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end  # 跨天窗口
    if start:
        return now >= start
    return now <= end


def run_monitor() -> dict:
    """执行一轮监控。返回统计摘要。"""
    if not _within_scan_window():
        log.info("当前不在扫描时间窗口内，跳过本轮扫描")
        return {"keywords": 0, "found": 0, "pushed": 0, "errors": 0, "skipped": "扫描窗口外"}

    keywords = query("SELECT * FROM keywords WHERE enabled=1")
    summary = {"keywords": len(keywords), "found": 0, "pushed": 0, "errors": 0}
    log.info("开始监控，关键词数=%d", len(keywords))

    realtime = config.PUSH_MODE in ("realtime", "both")
    for kw in keywords:
        try:
            engines = [e for e in (kw["platforms"] or "").split(",") if e] or None
            results, errors = search_all(kw["keyword"], engines)
            if errors:
                summary["errors"] += len(errors)

            new_items = []
            for it in results:
                h = _url_hash(it["url"])
                exists = query(
                    "SELECT id FROM results WHERE keyword_id=? AND url_hash=?",
                    (kw["id"], h),
                )
                if exists:
                    continue
                rid = execute(
                    "INSERT INTO results(keyword_id,title,url,url_hash,snippet,source_platform,engine)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (
                        kw["id"],
                        it["title"],
                        it["url"],
                        h,
                        it["snippet"],
                        it["source_platform"],
                        it["engine"],
                    ),
                )
                new_items.append(
                    {"id": rid, "title": it["title"], "url": it["url"],
                     "snippet": it["snippet"], "source_platform": it["source_platform"]}
                )

            if not new_items:
                continue

            # 情感分类
            cls = classify_batch(
                [{"title": i["title"], "snippet": i["snippet"]} for i in new_items]
            )
            for item, (sent, score, reason) in zip(new_items, cls):
                execute(
                    "UPDATE results SET sentiment=?, score=?, reason=? WHERE id=?",
                    (sent, score, reason, item["id"]),
                )
                item.update(sentiment=sent, score=score, reason=reason)

            summary["found"] += len(new_items)

            # 实时推送（受推送模式与时间窗口控制）
            if realtime and _within_window():
                push_res, pushed_n = push_findings(kw["keyword"], new_items)
                if pushed_n:
                    execute(
                        "INSERT INTO push_log(keyword_id,result_ids,channel,status,message) VALUES(?,?,?,?,?)",
                        (
                            kw["id"],
                            ",".join(str(i["id"]) for i in new_items),
                            "wecom",
                            "ok" if push_res.get("ok") else "fail",
                            push_res.get("error", ""),
                        ),
                    )
                    if push_res.get("ok"):
                        execute(
                            "UPDATE results SET pushed=1 WHERE id IN (%s)"
                            % ",".join(str(i["id"]) for i in new_items)
                        )
                        summary["pushed"] += pushed_n
        except Exception as e:  # noqa: BLE001
            log.exception("关键词 %s 处理失败", kw["keyword"])
            summary["errors"] += 1

    log.info("监控完成: %s", summary)
    return summary


def run_digest() -> dict:
    """定时汇总推送：把「尚未推送」的舆情按关键词汇总推送。"""
    rows = query(
        """SELECT r.id, r.title, r.url, r.snippet, r.source_platform,
                  r.sentiment, r.level, k.keyword
           FROM results r LEFT JOIN keywords k ON k.id=r.keyword_id
           WHERE r.pushed=0 ORDER BY r.id ASC"""
    )
    if not rows:
        log.info("定时汇总：无待推送舆情")
        return {"pushed": 0}
    by_keyword = {}
    for r in rows:
        by_keyword.setdefault(r["keyword"] or "未分类", []).append(dict(r))

    res, total = push_digest(by_keyword)
    ids = [str(r["id"]) for r in rows]
    execute(
        "INSERT INTO push_log(keyword_id,result_ids,channel,status,message) VALUES(?,?,?,?,?)",
        (0, ",".join(ids), "wecom", "ok" if res.get("ok") else "fail", res.get("error", "")),
    )
    if res.get("ok"):
        execute("UPDATE results SET pushed=1 WHERE id IN (%s)" % ",".join(ids))
    log.info("定时汇总推送完成: 推送 %d 条", total)
    return {"pushed": total}


def _digest_hour_minute() -> tuple:
    try:
        h, m = (config.PUSH_TIME or "09:00").split(":")
        return int(h), int(m)
    except Exception:  # noqa: BLE001
        return 9, 0


def _schedule_digest() -> None:
    if config.PUSH_MODE not in ("scheduled", "both"):
        return
    h, m = _digest_hour_minute()
    scheduler.add_job(
        run_digest,
        CronTrigger(hour=h, minute=m, timezone="Asia/Shanghai"),
        id="digest",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    log.info("定时汇总已启用，每天 %02d:%02d 推送", h, m)


def start() -> None:
    scheduler.add_job(
        run_monitor,
        "interval",
        minutes=config.MONITOR_INTERVAL_MINUTES,
        id="monitor",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    _schedule_digest()
    scheduler.start()
    log.info("调度器已启动，间隔 %d 分钟", config.MONITOR_INTERVAL_MINUTES)


def reschedule(minutes: int) -> None:
    """动态更新监控间隔，无需重启服务。"""
    if not minutes or minutes <= 0:
        return
    try:
        scheduler.reschedule_job("monitor", trigger="interval", minutes=minutes)
        config.MONITOR_INTERVAL_MINUTES = minutes
        log.info("监控间隔已更新为 %d 分钟", minutes)
    except Exception as e:  # noqa: BLE001
        log.warning("更新监控间隔失败: %s", e)


def reschedule_digest() -> None:
    """动态更新定时汇总任务（推送模式/时间变更时调用）。"""
    try:
        job = scheduler.get_job("digest")
        if job:
            scheduler.remove_job("digest")
        _schedule_digest()
    except Exception as e:  # noqa: BLE001
        log.warning("更新定时汇总任务失败: %s", e)


def trigger_now() -> dict:
    """手动触发一轮监控（在后台线程执行）。"""
    from threading import Thread

    result = {}

    def _run():
        result["summary"] = run_monitor()

    t = Thread(target=_run, daemon=True)
    t.start()
    return {"started": True}
