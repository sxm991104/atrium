"""飞书平台 —— 用官方 SDK `lark-oapi`。

## 为什么用 SDK 不用自己拼 HTTP

    第一版是自己 POST /open-apis/callback/ws/endpoint 拿 ws 地址。
    结果 404 —— 那个路径不对，而且飞书的长连接不是普通 HTTP 接口。

    实测（2026-11-04）：
        {"code": 404, "msg": "http error"}

    官方推荐用 lark-oapi，它把握手、心跳、重连全封装了：

        import lark_oapi as lark
        ws = lark.ws.Client(app_id, app_secret, event_handler=handler)
        ws.start()

    ⚠️ 代价是多一个依赖。但它是飞书官方包，
       协议变了它跟着改 —— 比自己维护强得多。

## ⚠️ 长连接要在飞书后台开启

    飞书开放平台 → 你的应用 → 事件与回调 → 订阅方式
        ↓
    选 **「使用长连接接收事件」**
        ↓
    不选这个，SDK 连上也收不到事��

## 依赖

    pip install lark-oapi
"""

from __future__ import annotations

import json
import threading
from typing import Callable

from atrium.core.message import Message, Reply
from atrium.platforms.base import Platform, PlatformError


class Feishu(Platform):
    """飞书平台。"""

    name = "feishu"

    def __init__(self, app_id: str, app_secret: str,
                 verify_ssl: bool = True) -> None:
        super().__init__()
        if not app_id or not app_secret:
            raise PlatformError("飞书缺 app_id / app_secret")

        self.app_id = app_id
        self.app_secret = app_secret
        self.verify_ssl = verify_ssl

        self._client = None      # lark.Client（发消息用）
        self._ws = None          # lark.ws.Client（收消息用）
        self._stop = threading.Event()

    # ══════════════════════════════════════════════════
    # 客户端（发消息用）
    # ══════════════════════════════════════════════════

    def _ensure_client(self):
        """造一个 lark.Client。"""
        if self._client is not None:
            return self._client

        try:
            import lark_oapi as lark  # noqa: PLC0415
        except ImportError as e:
            raise PlatformError(
                "缺 lark-oapi。装一下：pip install lark-oapi"
            ) from e

        self._client = lark.Client.builder() \
            .app_id(self.app_id) \
            .app_secret(self.app_secret) \
            .log_level(lark.LogLevel.WARNING) \
            .build()
        return self._client

    # ══════════════════════════════════════════════════
    # 发消息
    # ══════════════════════════════════════════════════

    def send(self, reply: Reply) -> bool:
        """发一条文字消息。

        ⚠️ 官方 SDK 的写法跟裸 HTTP 不一样：

            request = CreateMessageRequest.builder() \\
                .receive_id_type("chat_id") \\
                .request_body(CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("text")
                    .content(json.dumps({"text": ...}))
                    .build()) \\
                .build()

            response = client.im.v1.message.create(request)
            response.success()

        content 依然是 JSON **字符串**。
        """
        chat_id = (reply.channel or "").strip()
        text = (reply.text or "").strip()
        if not chat_id or not text:
            return False

        try:
            import lark_oapi as lark  # noqa: PLC0415
            from lark_oapi.api.im.v1 import (  # noqa: PLC0415
                CreateMessageRequest,
                CreateMessageRequestBody,
            )

            client = self._ensure_client()

            body = CreateMessageRequestBody.builder() \
                .receive_id(chat_id) \
                .msg_type("text") \
                .content(json.dumps({"text": text}, ensure_ascii=False)) \
                .build()

            req = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(body) \
                .build()

            resp = client.im.v1.message.create(req)
            if resp.success():
                return True

            # 打出来，排查用
            print(f"[feishu] 发失败 code={resp.code} msg={resp.msg}",
                  flush=True)
            return False

        except Exception as e:  # noqa: BLE001
            print(f"[feishu] 发消息异常：{type(e).__name__}: {e}", flush=True)
            return False

    # ══════════════════════════════════════════════════
    # 收消息（长连接）
    # ══════════════════════════════════════════════════

    def start(self, on_message: Callable[[Message], None]) -> None:
        """连上飞书长连接，开始收消息。

        ⚠️ 阻塞。要停就调 stop()。
        """
        self._on_message = on_message
        self._running = True
        self._stop.clear()

        try:
            import lark_oapi as lark  # noqa: PLC0415
        except ImportError as e:
            raise PlatformError(
                "缺 lark-oapi。装一下：pip install lark-oapi"
            ) from e

        # 事件处理器：只关心收消息
        def do_message(data) -> None:
            try:
                self._on_lark_message(data)
            except Exception as e:  # noqa: BLE001
                print(f"[feishu] 处理消息出错：{type(e).__name__}: {e}",
                      flush=True)

        handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(do_message) \
            .build()

        ws = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.WARNING,
        )
        self._ws = ws

        print("[feishu] 连长连接……", flush=True)

        # ⚠️ ws.Client.start() 是阻塞的。
        #    它内部会自己重连。
        ws.start()

    def _on_lark_message(self, data) -> None:
        """收到一条飞书消息事件。"""
        ev = getattr(data, "event", None)
        if ev is None:
            return

        m = getattr(ev, "message", None)
        if m is None:
            return

        chat_id = getattr(m, "chat_id", "") or ""
        if not chat_id:
            return

        sender = getattr(ev, "sender", None)
        open_id = ""
        if sender is not None:
            sid = getattr(sender, "sender_id", None)
            if sid is not None:
                open_id = getattr(sid, "open_id", "") or ""

        mtype = getattr(m, "message_type", "") or ""
        content = getattr(m, "content", "") or ""

        text = ""
        try:
            c = json.loads(content) if content else {}
            if mtype == "text":
                text = str(c.get("text") or "")
        except json.JSONDecodeError:
            pass

        text = _strip_mentions(text)

        msg = Message(
            platform=self.name,
            channel=chat_id,
            sender=open_id,
            text=text,
            reply_to=getattr(m, "message_id", "") or "",
            meta={
                "chat_type": getattr(m, "chat_type", "") or "",
                "message_type": mtype,
                "message_id": getattr(m, "message_id", "") or "",
                "open_id": open_id,
            },
            raw={
                "chat_id": chat_id,
                "message_id": getattr(m, "message_id", "") or "",
            },
        )
        self._emit(msg)

    # ══════════════════════════════════════════════════
    # 停止
    # ══════════════════════════════════════════════════

    def stop(self) -> None:
        self._stop.set()
        self._running = False
        # lark 的 ws.Client 没有 stop() —— 它是阻塞的，
        # 主线程退出时进程会跟着结束。

    def health(self) -> dict:
        return {
            "name": self.name,
            "running": self._running,
            "has_ws": self._ws is not None,
        }


def _strip_mentions(text: str) -> str:
    """去掉 @_user_1 @_all 这类占位符。"""
    if not text:
        return ""

    import re

    t = re.sub(r"@_user_\d+", "", text)
    t = t.replace("@_all", "")
    return t.strip()
