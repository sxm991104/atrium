"""落盘队列 —— 消息先进这里，再慢慢处理。

## 为什么一定要落盘

    平台有超时。飞书 3 秒不回就重发，5 次之后放弃。
    扣子也类似。

    如果收到消息 → 同步等引擎跑完（可能 2 分钟）→ 才回"收到"
        ↓
    平台早就判超时了
        ↓
    用户看到"发送失败"
        ↓
    **而且引擎那边还在跑，结果没处送**

    所以顺序必须是：

        收到消息
            ↓
        立刻写盘
            ↓
        立刻回"我收到了"（给平台看，不是给用户）
            ↓
        后台慢慢处理

## ⚠️ 为什么用 jsonl 而不是数据库

    一、不依赖任何东西。sqlite 也行，但 jsonl 用 cat 就能看。
    二、追加写是原子的。多个进程同时写也不会坏。
    三、坏了能手工修。

    代价是查询慢。但这里的量很小 ——
    一个人一天几百条算多了。

## 目录结构

    ~/.atrium/
        inbox.jsonl     收到的消息，待处理
        outbox.jsonl    要发的消息，待发送
        done.jsonl      处理完的（留个底，方便查）
        failed.jsonl    处理失败的
"""

from __future__ import annotations

import json
import os
import threading
from typing import Iterator

from atrium.core.message import Message, Reply


def _default_dir() -> str:
    """默认的落盘目录。"""
    home = os.path.expanduser("~")
    return os.path.join(home, ".atrium")


class Queue:
    """一个 jsonl 落盘队列。

    ⚠️ 线程安全：用锁保护追加写。
        多线程同时 append 时，json 行会被写串。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)

    # ── 写 ──

    def append(self, obj: dict) -> None:
        """追加一条。"""
        line = json.dumps(obj, ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                # ⚠️ fsync 有点慢，但断电时不丢消息比快重要。
                #    一天几百条的量，这点开销可以接受。
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass

    # ── 读 ──

    def read_all(self) -> list[dict]:
        """读全部。

        ⚠️ 坏行跳过，不整个失败。
            一行写坏了（比如写到一半断电），不能让整个队列读不出来。
        """
        out: list[dict] = []
        if not os.path.exists(self.path):
            return out
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(d, dict):
                        out.append(d)
        except OSError:
            pass
        return out

    def __iter__(self) -> Iterator[dict]:
        return iter(self.read_all())

    def __len__(self) -> int:
        return len(self.read_all())

    # ── 清 ──

    def clear(self) -> None:
        with self._lock:
            try:
                os.remove(self.path)
            except OSError:
                pass


class Bus:
    """收发总线：三个队列 + 两边的读写。

        inbox    平台 → 引擎
        outbox   引擎 → 平台
        done     处理完的留底
        failed   处理失败的留底
    """

    def __init__(self, base_dir: str = "") -> None:
        d = base_dir or _default_dir()
        os.makedirs(d, exist_ok=True)
        self.dir = d

        self.inbox = Queue(os.path.join(d, "inbox.jsonl"))
        self.outbox = Queue(os.path.join(d, "outbox.jsonl"))
        self.done = Queue(os.path.join(d, "done.jsonl"))
        self.failed = Queue(os.path.join(d, "failed.jsonl"))

    # ── 收 ──

    def push_message(self, msg: Message) -> None:
        """平台收到消息 → 写进 inbox。"""
        self.inbox.append(msg.to_dict())

    def pop_messages(self) -> list[Message]:
        """取走 inbox 里所有消息，清空。"""
        rows = self.inbox.read_all()
        if rows:
            self.inbox.clear()
        return [Message.from_dict(r) for r in rows]

    # ── 发 ──

    def push_reply(self, reply: Reply) -> None:
        """引擎产出结果 → 写进 outbox。"""
        self.outbox.append(reply.to_dict())

    def pop_replies(self) -> list[Reply]:
        """取走 outbox 里所有回复，清空。"""
        rows = self.outbox.read_all()
        if rows:
            self.outbox.clear()
        return [Reply.from_dict(r) for r in rows]

    # ── 留底 ──

    def mark_done(self, obj: dict, note: str = "") -> None:
        self.done.append({**obj, "_note": note})

    def mark_failed(self, obj: dict, err: str) -> None:
        self.failed.append({**obj, "_error": err})
