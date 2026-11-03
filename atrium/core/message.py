"""内部消息格式 —— 所有平台的公共语言。

## 为什么要一层统一格式

    飞书的消息长这样：
        {"event": {"message": {"chat_id": "oc_xxx", "content": "{\\"text\\":\\"你好\\"}"}}}

    扣子的长这样：
        {"sessionId": "sess_1", "prompt": [{"type": "text", "text": "你好"}]}

    QQ 又一样。

    如果总线、队列、引擎都直接吃平台格式，
    那加一个平台就要改所有地方。

    所以中间放一层：

        平台格式  →  Message  →  引擎
        引擎结果  →  Reply    →  平台格式

    两边都只认中间这层。

## ⚠️ raw 一定要留

    平台的字段千奇百怪，而且经常变。
    出问题的时候，`raw` 是唯一能复现现场的东西。

    实测教训（remote-dispatch，2026-11-04）：
        扣子那边的消息里有 <coze-context> 块，
        里面藏着 agent_id 和 message_id ——
        这些是主动推送必须的。
        如果当时只存了"清洗过的文字"，这些 ID 就永久丢了。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


def _now() -> str:
    """本地时间，带时区。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _new_id() -> str:
    """本地唯一 id。

    ⚠️ 不用平台给的 id 当主键。
       同一个消息可能在平台侧重发，也可能跨平台冲突。
       本地生成，平台 id 存在 raw 里。
    """
    return uuid.uuid4().hex[:16]


@dataclass
class Message:
    """一条进来的消息。

    所有平台的消息都转成这个，再进总线。
    """

    # ── 身份 ──
    id: str = field(default_factory=_new_id)
    platform: str = ""              # feishu / coze / qq / ...
    channel: str = ""               # 哪个会话（平台原样，比如 oc_xxx）
    sender: str = ""                # 谁发的（平台原样）

    # ── 内容 ──
    text: str = ""                  # 说的话
    attachments: list = field(default_factory=list)
    reply_to: str = ""              # 回哪条（平台原样，可能为空）

    # ── 时间 ──
    ts: str = field(default_factory=_now)

    # ── 原始数据 ──
    # ⚠️ 一定要留。见文件头说明。
    raw: dict = field(default_factory=dict)

    # ── 平台侧的元信息 ──
    # 比如扣子的 agent_id / message_id，飞书的 chat_type
    # 主动推送要靠这些
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "platform": self.platform,
            "channel": self.channel,
            "sender": self.sender,
            "text": self.text,
            "attachments": self.attachments,
            "reply_to": self.reply_to,
            "ts": self.ts,
            "raw": self.raw,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        return cls(
            id=d.get("id") or _new_id(),
            platform=d.get("platform") or "",
            channel=d.get("channel") or "",
            sender=d.get("sender") or "",
            text=d.get("text") or "",
            attachments=d.get("attachments") or [],
            reply_to=d.get("reply_to") or "",
            ts=d.get("ts") or _now(),
            raw=d.get("raw") or {},
            meta=d.get("meta") or {},
        )

    def short(self, n: int = 40) -> str:
        """一句话摘要，日志用。"""
        t = (self.text or "").replace("\n", " ").strip()
        if len(t) > n:
            t = t[:n] + "…"
        return f"[{self.platform}] {t}"


@dataclass
class Reply:
    """一条要发回去的消息。

    ⚠️ 为什么不直接复用 Message：
        Reply 需要记住"回到哪条"，而 Message 的 reply_to 是平台的 id。
        分开之后，引擎只要能产出 Reply，不用懂平台。
    """

    # ── 回到哪 ──
    platform: str = ""
    channel: str = ""
    reply_to: str = ""              # 对应哪条 Message（平台的 id）

    # ── 内容 ──
    text: str = ""
    attachments: list = field(default_factory=list)

    # ── 元信息 ──
    # 比如扣子的 cozeAgentId / cozeTriggerMsgId（主动推送要用）
    meta: dict = field(default_factory=dict)

    id: str = field(default_factory=_new_id)
    ts: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "platform": self.platform,
            "channel": self.channel,
            "reply_to": self.reply_to,
            "text": self.text,
            "attachments": self.attachments,
            "meta": self.meta,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Reply":
        return cls(
            id=d.get("id") or _new_id(),
            platform=d.get("platform") or "",
            channel=d.get("channel") or "",
            reply_to=d.get("reply_to") or "",
            text=d.get("text") or "",
            attachments=d.get("attachments") or [],
            meta=d.get("meta") or {},
            ts=d.get("ts") or _now(),
        )

    @classmethod
    def to(cls, msg: Message, text: str) -> "Reply":
        """直接从一条 Message 构造回复。

        把 platform / channel / reply_to / meta 全带过去 ——
        这几个字段是"回得到原处"的关键，少一个就回不去了。
        """
        return cls(
            platform=msg.platform,
            channel=msg.channel,
            reply_to=msg.reply_to or msg.id,
            text=text,
            meta=dict(msg.meta),
        )


def preview(text: str, n: int = 60) -> str:
    """日志里显示一段文字。"""
    t = (text or "").replace("\n", "⏎").strip()
    if len(t) > n:
        t = t[:n] + "…"
    return t
