"""钉钉平台。

## 两部分

    收消息   Stream 模式（长连接，不用公网地址）
    发消息   sessionWebhook（SDK 的 reply_text 帮你做了）

## 为什么用 Stream 而不是 HTTP 回调

    HTTP 回调要求钉钉能访问到你的机器 ——
    要么有公网 IP，要么内网穿透。

    Stream 是我们主动连钉钉 ——
    用户在自己家宽带后面也能用。

    ⚠️ 这跟飞书的长连接是同一个思路。
       飞书用 lark-oapi，钉钉用 dingtalk-stream。

## ⚠️ 依赖已装

    dingtalk-stream 0.24.3

## ⚠️ 主动推送

    sessionWebhook 有效期约 1 小时，过期就不能用了。
    所以"任务跑完主动推"要分两种情况：

        · 1 小时内   → 直接用 sessionWebhook
        · 超过       → 用机器人主动发消息接口（要 robotCode）

    现在两个都实现了，先试 webhook，失败再试主动。

## 消息格式（钉钉推来的）

    {
        "msgtype": "text",
        "text": {"content": "你好"},
        "senderStaffId": "...",
        "senderNick": "...",
        "conversationId": "cid...",
        "conversationType": "1",     # 1=单聊 2=群聊
        "msgId": "...",
        "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?..."
    }
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request

from atrium.core.message import Message, Reply
from atrium.platforms.base import Platform, PlatformError
from atrium.platforms.registry import register


def _looks_like_markdown(text: str) -> bool:
    """这段文字里有没有 Markdown 语法。

    ⚠️ 只看有没有"能明显看出是 markdown"的痕迹：

        · 表格："| xxx | yyy |"
        · 标题："## xxx"
        · 代码块：```xxx```
        · 列表："- xxx" 或 "1. xxx"

    为什么要判断：
        纯闲聊"嗯我在呢"用 markdown 发，钉钉会渲染成卡片，
        看起来像通知，不像聊天。
    """
    if not text:
        return False

    # ⚠️ 加粗也算。
    #
    #    实测（2026-11-04）：
    #        监工推的消息里有 "**4 个任务跑完了**"，
    #        但判断函数没认出来（只看了 | ## ``` -），
    #        结果走 reply_text 发出去了，
    #        用户看到的就是一堆星号。
    if "**" in text:
        return True

    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("|") and s.count("|") >= 2:
            return True
        if s.startswith("## ") or s.startswith("### "):
            return True
        if s.startswith("```"):
            return True
        if s.startswith("- ") or s.startswith("* "):
            return True
        # "1. xxx"
        if len(s) > 3 and s[0].isdigit() and s[1:3] in (". ", ") "):
            return True

    return False


@register("dingtalk")
def _make(cfg: dict) -> Platform:
    """按配置造一个钉钉平台。"""
    return DingTalk(
        cfg.get("client_id") or cfg.get("app_key") or "",
        cfg.get("client_secret") or cfg.get("app_secret") or "",
        verify_ssl=bool(cfg.get("verify_ssl", True)),
    )


class DingTalk(Platform):
    """钉钉平台。"""

    name = "dingtalk"

    def __init__(self, client_id: str, client_secret: str,
                 verify_ssl: bool = True) -> None:
        super().__init__()
        if not client_id or not client_secret:
            raise PlatformError("钉钉缺 client_id / client_secret")

        self.client_id = client_id
        self.client_secret = client_secret

        self._token = ""
        self._token_expire = 0.0
        self._client = None
        self._stop = threading.Event()

        # 用过的 handler（回消息要用）
        self._handlers: list = []

        if verify_ssl:
            self._ctx = ssl.create_default_context()
        else:
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

        # 会话 → 最近一条原始消息（回消息要用完整的 incoming_message）
        self._last_msg: dict[str, object] = {}

        # 会话 → sessionWebhook（**发文件要用**）
        #
        # ⚠️ 重建这个类的时候漏了这一行。
        #
        #    send_file 里调 self._webhooks 直接 AttributeError，
        #    而这个异常被上层的 try/except 吞掉了 ——
        #    表面上看就是"文件压根没发出去"。
        #
        #    实测（2026-11-04）：
        #        查了半天以为上传接口有问题，
        #        单独测上传是通的（media_id 拿到了），
        #        真正的原因就是这里少了一个属性。
        self._webhooks: dict[str, str] = {}

    # ══════════════════════════════════════════════════
    # 发消息
    # ══════════════════════════════════════════════════

    def send(self, reply: Reply) -> bool:
        """发一条文字消息。

        ⚠️ 优先用 SDK 的 reply_text（它自己处理 sessionWebhook）。
           拿不到原始消息就退回手拼 webhook。
        """
        text = (reply.text or "").strip()
        if not text:
            return False

        channel = (reply.channel or "").strip()

        # 一、有原始消息 + handler → 用 SDK
        raw_msg = self._last_msg.get(channel)
        handler = self._handlers[-1] if self._handlers else None
        if raw_msg is not None and handler is not None:
            # ⚠️ 有 Markdown 语法就发 markdown，没有就发纯文本。
            #
            #    为什么不一律发 markdown：
            #        markdown 消息在钉钉里是"卡片"样式，
            #        一句话的闲聊用卡片反而怪。
            #
            #    实测（2026-11-04）：
            #        用户要表格，前台发了 "| 窗口 | 干什么 |" 这种，
            #        用 text 发出去就是原样一串竖线，没变成表。
            if _looks_like_markdown(text):
                try:
                    handler.reply_markdown("丫头", text, raw_msg)
                    return True
                except Exception as e:  # noqa: BLE001
                    print(f"[dingtalk] markdown 发失败，退回 text：{e}",
                          flush=True)

            try:
                handler.reply_text(text, raw_msg)
                return True
            except Exception as e:  # noqa: BLE001
                print(f"[dingtalk] SDK 回消息失败：{e}", flush=True)

        # 二、手拼 sessionWebhook
        hook = (reply.meta or {}).get("session_webhook") or ""
        if hook:
            if self._post_webhook(hook, text):
                return True

        # 三、主动推送
        return self._send_active(reply, text)

    def send_file(self, reply) -> bool:
        """发一个文件。

        ## 为什么单独一个方法

            Reply.attachments 里可能有本地路径，
            这些要**真的上传再发**，不是拼进文字。

        ## 流程

            一、上传拿 media_id
            二、用 sessionWebhook 发 file 消息

        ## ⚠️ 失败了不抛异常

            文件发不出去不该让整条推送失败。
            文字部分已经发出去了，用户在文字里能看到文件名。
        """
        print("[SF_DEBUG_ENTER] send_file 进来了", flush=True)

        paths = [str(p) for p in (reply.attachments or []) if p]
        print(f"[SF_DEBUG] paths={paths}", flush=True)
        if not paths:
            print("[SF_DEBUG] 没有路径，返回 True", flush=True)
            return True

        hook = (reply.meta or {}).get("session_webhook") or ""
        channel = (reply.channel or "").strip()
        print(f"[SF_DEBUG] meta_hook={hook[:50]!r} channel={channel!r}", flush=True)

        if not hook:
            hook = self._webhooks.get(channel) or ""
            print(f"[SF_DEBUG] 从缓存取的 hook={hook[:50]!r}", flush=True)

        if not hook:
            print("[dingtalk] 没有 sessionWebhook，文件发不了", flush=True)
            return False

        try:
            from atrium.platforms.dingtalk_file import upload_file
        except ImportError as e:
            print(f"[dingtalk] 载不进来 dingtalk_file：{e}", flush=True)
            return False

        ok_any = False
        for path in paths:
            print(f"[SF_DEBUG] 开始上传 {path}", flush=True)
            try:
                media_id = upload_file(
                    path, self.client_id, self.client_secret, self._ctx
                )
                print(f"[SF_DEBUG] 上传成功 media_id={media_id}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[dingtalk] 上传 {path} 失败：{type(e).__name__}: {e}",
                      flush=True)
                continue

            # ⚠️ 钉钉要三个字段，名字全是驼峰，少一个都不收。
            #
            #    挨个踩出来的报错：
            #        {"errcode": 401102, "errmsg": "miss param : file->mediaId"}
            #        {"errcode": 401104, "errmsg": "miss param : file->fileType"}
            #        {"errcode": 401103, "errmsg": "miss param : file->fileName"}
            #
            #    三个都齐了才真的发出去：
            #        {"errcode": 0, "errmsg": "ok"}
            #
            #    实测（2026-11-04）：
            #        前面一直以为上传接口有问题，
            #        其实是发消息这一步的字段名不对，钉钉只回一个错误码，
            #        而上层又把异常吞了，看起来就像"文件压根没发"。
            body = {
                "msgtype": "file",
                "file": {
                    "mediaId": media_id,
                    "fileType": "file",
                    "fileName": os.path.basename(path),
                },
            }
            try:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    hook, data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30,
                                            context=self._ctx) as r:
                    d = json.loads(r.read())
                if (d.get("errcode") == 0) or (d.get("code") == 0):
                    ok_any = True
                    print(f"[dingtalk] 发出文件 {path}", flush=True)
                else:
                    # ⚠️ 失败也打出来。
                    #    以前这里不打印，钉钉回的错误码全被吞了，
                    #    排查时两眼一黑。
                    print(f"[dingtalk] 发文件被拒："
                          f"{json.dumps(d, ensure_ascii=False)[:200]}",
                          flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[dingtalk] 发文件失败：{type(e).__name__}: {e}",
                      flush=True)

        return ok_any

    def _post_webhook(self, hook: str, text: str) -> bool:
        """用 sessionWebhook 回一条。"""
        body = {"msgtype": "text", "text": {"content": text}}
        try:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                hook, data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20, context=self._ctx) as r:
                d = json.loads(r.read())
            return (d.get("errcode") == 0) or (d.get("code") == 0)
        except Exception:  # noqa: BLE001
            return False

    def _send_active(self, reply: Reply, text: str) -> bool:
        """主动发消息（webhook 过期时用）。"""
        meta = reply.meta or {}
        user_id = meta.get("sender_staff_id") or ""
        conv_type = str(meta.get("conversation_type") or "")

        try:
            tok = self._get_token()
        except PlatformError:
            return False

        if conv_type == "2" and reply.channel:
            url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
            body = {
                "openConversationId": reply.channel,
                "msgKey": "sampleText",
                "msgParam": json.dumps({"content": text}, ensure_ascii=False),
            }
        else:
            if not user_id:
                return False
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
            body = {
                "robotCode": self.client_id,
                "userIds": [user_id],
                "msgKey": "sampleText",
                "msgParam": json.dumps({"content": text}, ensure_ascii=False),
            }

        try:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={
                    "Content-Type": "application/json",
                    "x-acs-dingtalk-access-token": tok,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20, context=self._ctx) as r:
                d = json.loads(r.read())
            return bool(
                d.get("processQueryKey")
                or d.get("taskId")
                or d.get("errcode") == 0
                or d.get("code") == "0"
            )
        except Exception as e:  # noqa: BLE001
            print(f"[dingtalk] 主动推送失败：{type(e).__name__}: {e}", flush=True)
            return False

    # ══════════════════════════════════════════════════
    # token
    # ══════════════════════════════════════════════════

    def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expire - 60:
            return self._token

        body = json.dumps({
            "appKey": self.client_id,
            "appSecret": self.client_secret,
        }).encode("utf-8")

        try:
            req = urllib.request.Request(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20, context=self._ctx) as r:
                d = json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            raise PlatformError(f"换 token 失败：{e}") from e

        tok = d.get("accessToken") or ""
        if not tok:
            raise PlatformError(
                f"换 token 失败：{json.dumps(d, ensure_ascii=False)[:200]}"
            )

        self._token = tok
        self._token_expire = now + int(d.get("expireIn") or 7200)
        return tok

    # ══════════════════════════════════════════════════
    # 收消息（Stream）
    # ══════════════════════════════════════════════════

    def start(self, on_message) -> None:
        """连上钉钉 Stream，开始收消息。

        ⚠️ 阻塞。要停就调 stop()。
        """
        self._on_message = on_message
        self._running = True
        self._stop.clear()

        try:
            import dingtalk_stream  # noqa: PLC0415
            from dingtalk_stream import ChatbotHandler, ChatbotMessage  # noqa: PLC0415
        except ImportError as e:
            raise PlatformError(
                "缺 dingtalk-stream。装一下：pip install dingtalk-stream"
            ) from e

        outer = self

        class _Handler(ChatbotHandler):
            async def process(self, callback):
                try:
                    data = callback.data
                    msg = ChatbotMessage.from_dict(data)
                    outer._handlers.append(self)
                    outer._on_chatbot_message(msg, data)
                except Exception as e:  # noqa: BLE001
                    import traceback

                    traceback.print_exc()
                    print(f"[dingtalk] 处理消息出错：{type(e).__name__}: {e}",
                          flush=True)

                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        credential = dingtalk_stream.Credential(
            self.client_id, self.client_secret
        )
        client = dingtalk_stream.DingTalkStreamClient(credential)
        client.register_callback_handler(ChatbotMessage.TOPIC, _Handler())
        self._client = client

        print("[dingtalk] 连 Stream……", flush=True)
        client.start_forever()

    def _on_chatbot_message(self, msg, data: dict) -> None:
        """收到一条机器人消息。"""
        sender_id = getattr(msg, "sender_staff_id", "") or ""
        sender_nick = getattr(msg, "sender_nick", "") or ""
        conv_id = getattr(msg, "conversation_id", "") or ""
        conv_type = str(getattr(msg, "conversation_type", "") or "")
        msg_id = (getattr(msg, "message_id", "") or "")
        hook = getattr(msg, "session_webhook", "") or ""

        # 文本
        text = ""
        t = getattr(msg, "text", None)
        if t is not None:
            text = getattr(t, "content", "") or ""
        if not text:
            text = (data.get("text") or {}).get("content") or ""

        channel = conv_id or sender_id
        if not channel:
            return

        # 记下来，回消息要用
        self._last_msg[channel] = msg

        out = Message(
            platform=self.name,
            channel=channel,
            sender=sender_id,
            text=text,
            reply_to=msg_id,
            meta={
                "conversation_type": conv_type,
                "conversation_id": conv_id,
                "sender_staff_id": sender_id,
                "sender_nick": sender_nick,
                "session_webhook": hook,
            },
            raw=data,
        )
        self._emit(out)

    # ══════════════════════════════════════════════════
    # 停止
    # ══════════════════════════════════════════════════

    def stop(self) -> None:
        self._stop.set()
        self._running = False

    def health(self) -> dict:
        return {
            "name": self.name,
            "running": self._running,
            "has_client": self._client is not None,
        }
