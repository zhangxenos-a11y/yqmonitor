"""舆情监测系统 — FastAPI 入口"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import config
from .db import execute, init_db, query, query_one
from .push import send_test
from .scheduler import run_monitor, start
from .security import hash_password, make_session, read_session, verify_password

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("yqmonitor")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
COOKIE_NAME = "yq_session"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start()
    from .wecom_bot import bot_client

    bot_client.start()
    yield


app = FastAPI(title="舆情监测系统", version=__version__, lifespan=lifespan)


# ---------- 认证 ----------
def get_current_user(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    uid = read_session(token) if token else None
    if not uid:
        raise HTTPException(status_code=401, detail="未登录")
    user = query_one("SELECT * FROM users WHERE id=?", (uid,))
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# ---------- 认证接口 ----------
@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    user = query_one("SELECT * FROM users WHERE username=?", (username,))
    if not user or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    resp = JSONResponse({"ok": True, "username": user["username"], "role": user["role"]})
    resp.set_cookie(COOKIE_NAME, make_session(user["id"]), httponly=True, samesite="lax", max_age=7 * 24 * 3600)
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


@app.get("/api/me")
async def me(user=Depends(get_current_user)):
    return {"username": user["username"], "role": user["role"]}


# ---------- 关键词 ----------
@app.get("/api/keywords")
async def list_keywords(user=Depends(get_current_user)):
    rows = query("SELECT * FROM keywords ORDER BY id DESC")
    return [dict(r) for r in rows]


@app.post("/api/keywords")
async def create_keyword(request: Request, user=Depends(get_current_user)):
    body = await request.json()
    keyword = (body.get("keyword") or "").strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="关键词不能为空")
    platforms = ",".join([p for p in (body.get("platforms") or []) if p])
    kid = execute(
        "INSERT INTO keywords(user_id, keyword, enabled, platforms) VALUES(?,?,?,?)",
        (user["id"], keyword, 1, platforms),
    )
    return {"id": kid}


@app.put("/api/keywords/{kid}")
async def update_keyword(kid: int, request: Request, user=Depends(get_current_user)):
    body = await request.json()
    keyword = (body.get("keyword") or "").strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="关键词不能为空")
    platforms = ",".join([p for p in (body.get("platforms") or []) if p])
    execute(
        "UPDATE keywords SET keyword=?, platforms=?, enabled=? WHERE id=?",
        (keyword, platforms, 1 if body.get("enabled", True) else 0, kid),
    )
    return {"ok": True}


@app.delete("/api/keywords/{kid}")
async def delete_keyword(kid: int, user=Depends(get_current_user)):
    execute("DELETE FROM results WHERE keyword_id=?", (kid,))
    execute("DELETE FROM keywords WHERE id=?", (kid,))
    return {"ok": True}


@app.post("/api/keywords/{kid}/toggle")
async def toggle_keyword(kid: int, user=Depends(get_current_user)):
    row = query_one("SELECT enabled FROM keywords WHERE id=?", (kid,))
    if not row:
        raise HTTPException(status_code=404, detail="关键词不存在")
    new_val = 0 if row["enabled"] else 1
    execute("UPDATE keywords SET enabled=? WHERE id=?", (new_val, kid))
    return {"enabled": new_val}


# ---------- 舆情级别标注 ----------
@app.post("/api/results/{rid}/level")
async def set_level(rid: int, request: Request, user=Depends(get_current_user)):
    """人工标注舆情级别（一般/较大/重大/特别重大）。"""
    from .levels import LEVELS

    row = query_one("SELECT id FROM results WHERE id=?", (rid,))
    if not row:
        raise HTTPException(status_code=404, detail="舆情不存在")
    body = await request.json()
    level = (body.get("level") or "").strip()
    if level not in LEVELS:
        raise HTTPException(status_code=400, detail=f"级别必须是 {('/').join(LEVELS)} 之一")
    note = (body.get("note") or "").strip()
    execute("UPDATE results SET level=?, level_note=? WHERE id=?", (level, note, rid))
    return {"ok": True, "level": level}


# ---------- 监测结果 ----------
@app.get("/api/results")
async def list_results(
    keyword_id: int = 0,
    sentiment: str = "",
    platform: str = "",
    pushed: int = -1,
    q: str = "",
    page: int = 1,
    size: int = 20,
    user=Depends(get_current_user),
):
    where, params = ["1=1"], []
    if keyword_id:
        where.append("keyword_id=?")
        params.append(keyword_id)
    if sentiment:
        where.append("sentiment=?")
        params.append(sentiment)
    if platform:
        where.append("source_platform=?")
        params.append(platform)
    if pushed >= 0:
        where.append("pushed=?")
        params.append(pushed)
    if q:
        where.append("(title LIKE ? OR snippet LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    cond = " AND ".join(where)

    total = query_one(f"SELECT COUNT(*) AS c FROM results WHERE {cond}", params)["c"]
    offset = (page - 1) * size
    rows = query(
        f"""SELECT r.*, k.keyword FROM results r
            LEFT JOIN keywords k ON k.id=r.keyword_id
            WHERE {cond} ORDER BY r.id DESC LIMIT ? OFFSET ?""",
        params + [size, offset],
    )
    return {"total": total, "page": page, "size": size, "items": [dict(r) for r in rows]}


@app.get("/api/stats")
async def stats(user=Depends(get_current_user)):
    total = query_one("SELECT COUNT(*) AS c FROM results")["c"]
    by_sentiment = {
        r["sentiment"]: r["c"]
        for r in query("SELECT sentiment, COUNT(*) AS c FROM results GROUP BY sentiment")
    }
    by_platform = {
        r["source_platform"]: r["c"]
        for r in query("SELECT source_platform, COUNT(*) AS c FROM results GROUP BY source_platform ORDER BY c DESC LIMIT 10")
    }
    recent = [dict(r) for r in query("SELECT * FROM results ORDER BY id DESC LIMIT 10")]
    neg_count = sum(v for k, v in by_sentiment.items() if k == "负面")
    return {
        "total": total,
        "negative": neg_count,
        "by_sentiment": by_sentiment,
        "by_platform": by_platform,
        "recent": recent,
    }


# ---------- 推送记录 ----------
@app.get("/api/push-log")
async def push_log(page: int = 1, size: int = 20, user=Depends(get_current_user)):
    total = query_one("SELECT COUNT(*) AS c FROM push_log")["c"]
    rows = query(
        """SELECT pl.*, k.keyword FROM push_log pl
           LEFT JOIN keywords k ON k.id=pl.keyword_id
           ORDER BY pl.id DESC LIMIT ? OFFSET ?""",
        (size, (page - 1) * size),
    )
    items = []
    for r in rows:
        d = dict(r)
        # 解析该次推送包含的具体舆情内容
        ids = [int(x) for x in (d.get("result_ids") or "").split(",") if x.strip().isdigit()]
        if ids:
            ph = ",".join("?" * len(ids))
            d["results"] = [
                dict(x)
                for x in query(
                    f"SELECT id, title, url, sentiment, source_platform, found_at "
                    f"FROM results WHERE id IN ({ph})",
                    ids,
                )
            ]
        else:
            d["results"] = []
        items.append(d)
    return {"total": total, "items": items}


# ---------- 设置 ----------
def _setting_map():
    return {r["key"]: r["value"] for r in query("SELECT * FROM settings")}


@app.get("/api/settings")
async def get_settings(user=Depends(get_current_user)):
    s = _setting_map()
    return {
        "wecom_key": s.get("wecom_key", config.WECOM_WEBHOOK_KEY),
        "wecom_bot_id": s.get("wecom_bot_id", config.WECOM_BOT_ID),
        "wecom_bot_secret": s.get("wecom_bot_secret", config.WECOM_BOT_SECRET),
        "deepseek_key": s.get("deepseek_key", config.DEEPSEEK_API_KEY),
        "deepseek_model": s.get("deepseek_model", config.DEEPSEEK_MODEL),
        "interval": int(s.get("interval", config.MONITOR_INTERVAL_MINUTES)),
        "sources": [x for x in (s.get("sources", ",".join(config.SEARCH_SOURCES)).split(",")) if x],
        # 推送配置
        "push_mode": s.get("push_mode", config.PUSH_MODE),
        "push_time": s.get("push_time", config.PUSH_TIME),
        "push_window_start": s.get("push_window_start", config.PUSH_WINDOW_START),
        "push_window_end": s.get("push_window_end", config.PUSH_WINDOW_END),
        "push_fields": [x for x in (s.get("push_fields", ",".join(config.PUSH_FIELDS)).split(",")) if x],
        "push_batch_size": int(s.get("push_batch_size", config.PUSH_BATCH_SIZE)),
        "push_min_level": s.get("push_min_level", config.PUSH_MIN_LEVEL),
    }


@app.post("/api/settings")
async def save_settings(request: Request, user=Depends(get_current_user)):
    body = await request.json()
    for key in ("wecom_key", "wecom_bot_id", "wecom_bot_secret", "deepseek_key", "deepseek_model", "interval", "sources",
                "push_mode", "push_time", "push_window_start", "push_window_end", "push_fields", "push_batch_size", "push_min_level"):
        if key in body:
            val = body[key]
            if isinstance(val, list):
                val = ",".join(val)
            execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(val)),
            )
    # 让运行中的调度器/推送读取新配置
    m = _setting_map()
    config.WECOM_WEBHOOK_KEY = m.get("wecom_key", "")
    config.WECOM_BOT_ID = m.get("wecom_bot_id", "")
    config.WECOM_BOT_SECRET = m.get("wecom_bot_secret", "")
    config.DEEPSEEK_API_KEY = m.get("deepseek_key", "")
    config.DEEPSEEK_MODEL = m.get("deepseek_model", "deepseek-chat")
    config.PUSH_MODE = m.get("push_mode", config.PUSH_MODE)
    config.PUSH_TIME = m.get("push_time", config.PUSH_TIME)
    config.PUSH_WINDOW_START = m.get("push_window_start", "")
    config.PUSH_WINDOW_END = m.get("push_window_end", "")
    config.PUSH_FIELDS = [x for x in m.get("push_fields", ",".join(config.PUSH_FIELDS)).split(",") if x]
    config.PUSH_BATCH_SIZE = int(m.get("push_batch_size", config.PUSH_BATCH_SIZE))
    config.PUSH_MIN_LEVEL = m.get("push_min_level", config.PUSH_MIN_LEVEL)
    # 间隔变更即时生效
    from .scheduler import reschedule, reschedule_digest

    reschedule(int(m.get("interval", config.MONITOR_INTERVAL_MINUTES)))
    reschedule_digest()
    return {"ok": True}


@app.post("/api/run")
async def trigger_run(user=Depends(get_current_user)):
    """手动触发一轮监控。"""
    try:
        summary = run_monitor()
        return {"ok": True, "summary": summary}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@app.post("/api/digest")
async def trigger_digest(user=Depends(get_current_user)):
    """手动触发定时汇总推送。"""
    try:
        from .scheduler import run_digest

        summary = run_digest()
        return {"ok": True, "pushed": summary.get("pushed", 0)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@app.post("/api/test-push")
async def test_push(user=Depends(get_current_user)):
    return send_test()


@app.get("/api/wecom-chats")
async def wecom_chats(user=Depends(get_current_user)):
    from . import wecom_bot

    return {
        "bot_configured": bool(config.WECOM_BOT_ID and config.WECOM_BOT_SECRET),
        "connected": wecom_bot.bot_client.connected,
        "chats": wecom_bot.bot_client.get_chats(),
    }


@app.get("/api/sources")
async def sources(user=Depends(get_current_user)):
    from .search import ENGINES

    return {k: v["name"] for k, v in ENGINES.items()}


# ---------- 静态前端 ----------
@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
