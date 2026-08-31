"""企业微信「智能机器人」WebSocket 长连接客户端。

凭证为 Bot ID + Secret（非群机器人 webhook）。流程：
1. 建立 wss://openws.work.weixin.qq.com/ 长连接并订阅（aibot_subscribe）
2. 用户在企业微信里给机器人发消息时，收到 aibot_msg_callback 记录会话标识
3. 之后通过 aibot_send_msg 主动推送（body 顶层 chatid，msgtype 仅支持 markdown）

会话标识规则（官方 SDK 约定）：单聊用 from.userid，群聊用 chatid。
"""
import json
import logging
import threading
import time
import uuid

import websocket

from .config import config
from .db import execute, query

log = logging.getLogger("yqmonitor.wecom_bot")

WSS_URL = "wss://openws.work.weixin.qq.com/"


class WecomBot:
    def __init__(self):
        self.ws = None
        self.connected = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # ---------- 生命周期 ----------
    def start(self):
        if not (config.WECOM_BOT_ID and config.WECOM_BOT_SECRET):
            log.info("未配置智能机器人 Bot ID/Secret，跳过")
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self._connect()
            except Exception as e:  # noqa: BLE001
                log.warning("智能机器人连接异常: %s", e)
            self._stop.wait(5)

    def _connect(self):
        ws = websocket.create_connection(WSS_URL, timeout=30)
        self.ws = ws
        self._send({
            "cmd": "aibot_subscribe",
            "headers": {"req_id": self._req_id()},
            "body": {"bot_id": config.WECOM_BOT_ID, "secret": config.WECOM_BOT_SECRET},
        })
        self.connected = True
        log.info("智能机器人已订阅")
        ws.settimeout(1)
        last_ping = time.time()
        while not self._stop.is_set():
            try:
                msg = ws.recv()
                self._handle(msg)
            except websocket.WebSocketTimeoutException:
                if time.time() - last_ping >= 30:  # 心跳保活
                    try:
                        self._send({"cmd": "ping", "headers": {"req_id": self._req_id()}, "body": {}})
                        last_ping = time.time()
                    except Exception:  # noqa: BLE001
                        break
            except Exception as e:  # noqa: BLE001
                log.warning("智能机器人连接中断: %s", e)
                break
        self.connected = False
        self.ws = None
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass

    # ---------- 收发 ----------
    @staticmethod
    def _req_id():
        return str(uuid.uuid4())

    def _send(self, payload: dict) -> bool:
        with self._lock:
            if self.ws:
                try:
                    self.ws.send(json.dumps(payload, ensure_ascii=False))
                    return True
                except Exception as e:  # noqa: BLE001
                    log.warning("智能机器人发送失败: %s", e)
        return False

    def _handle(self, raw: str):
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return
        cmd = data.get("cmd")
        if cmd == "aibot_msg_callback":
            body = data.get("body") or {}
            chattype = body.get("chattype") or "single"
            if chattype == "group":
                target = body.get("chatid")
            else:  # 单聊：chatid 为空，用 from.userid 作为目标
                target = (body.get("from") or {}).get("userid")
            if target:
                self._remember(target, chattype)
                log.info("记录智能机器人会话 target=%s type=%s", target, chattype)
            else:
                log.warning("回调未取到会话标识: %s", raw[:200])
        else:
            # 订阅/心跳/发送的回执：errcode 非 0 说明出错
            errcode = data.get("errcode")
            if errcode not in (None, 0):
                log.warning("智能机器人响应错误 errcode=%s errmsg=%s", errcode, data.get("errmsg"))

    def _remember(self, target, chattype):
        rows = query("SELECT value FROM settings WHERE key='wecom_chats'")
        chats = []
        if rows:
            try:
                chats = json.loads(rows[0]["value"])
            except Exception:  # noqa: BLE001
                chats = []
        if not any(c.get("chatid") == target for c in chats):
            chats.append({"chatid": target, "chattype": chattype})
            execute(
                "INSERT INTO settings(key,value) VALUES('wecom_chats',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(chats),),
            )

    # ---------- 对外接口 ----------
    def get_chats(self):
        rows = query("SELECT value FROM settings WHERE key='wecom_chats'")
        if rows:
            try:
                return json.loads(rows[0]["value"])
            except Exception:  # noqa: BLE001
                return []
        return []

    def send_markdown(self, chatid, content: str) -> bool:
        """主动推送 markdown 消息。chatid：单聊填 userid，群聊填群 chatid。"""
        payload = {
            "cmd": "aibot_send_msg",
            "headers": {"req_id": self._req_id()},
            "body": {"chatid": chatid, "msgtype": "markdown", "markdown": {"content": content}},
        }
        return self._send(payload)


bot_client = WecomBot()
