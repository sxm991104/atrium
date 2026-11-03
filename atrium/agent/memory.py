"""对话记忆 —— 让前台记得刚才聊了什么。

## 为什么必须要有

    atrium 原来是**无状态**的：

        用户：给 deepseek 派个问时间的活
        前台：已经派下去了，叫「问几点DS」
        用户：看看进度
        前台：（不知道你指哪个）→ 列了全部 20 个任务
                ↓
        **用户看到一堆老任务，懵**

    因为每一轮都是独立的一次模型调用，
    前台根本不知道"刚才派了什么"。

## 怎么存

    按「平台 + 会话」分组：

        {("dingtalk", "cidxxx"): [ {role, content}, ... ]}

    只保留最近 N 轮（默认 10 轮 = 20 条消息）。
    太多了浪费 token，太少了记不住。

## ⚠️ 为什么不落盘

    对话记忆是**临时的**：
        · atrium 重启就没了
        · 这是有意的 —— 重启后重新开始，比记住半截上下文更安全

    真要持久化，以后加，但要考虑隐私（聊天内容不该默默写到盘上）。

## ⚠️ 为什么不存工具调用

    只存"用户说了什么"和"我回了什么"。

    工具调用（dispatch_task 的参数、返回）不进历史：
        · 太长，占 token
        · 而且回话里已经包含了关键信息（"派下去了，叫问几点DS"）

    如果用户追问细节（"那个任务编号是多少"），
    前台可以用 task_list 去查 —— 那时候再查是对的。
"""

from __future__ import annotations

import threading
import time


class Memory:
    """按会话分组的对话记忆。"""

    def __init__(self, max_turns: int = 10) -> None:
        # max_turns 是"轮"数，一轮 = 一问一答
        self.max_turns = max(1, int(max_turns))
        self._data: dict[tuple[str, str], list[dict]] = {}
        self._lock = threading.Lock()

    # ── 读写 ──

    def history(self, platform: str, channel: str) -> list[dict]:
        """拿某个会话的历史（已经是可以直接喂给模型的形式）。"""
        key = (platform or "", channel or "")
        with self._lock:
            rows = self._data.get(key) or []
            # ⚠️ 返回副本。
            #    直接返回原列表的话，调用方改它会影响存储。
            return [dict(r) for r in rows]

    def add_user(self, platform: str, channel: str, text: str) -> None:
        """记一条用户说的。"""
        self._push(platform, channel, {"role": "user", "content": text})

    def add_assistant(self, platform: str, channel: str, text: str) -> None:
        """记一条前台回的。

        ⚠️ 空回答不记 —— 记了反而让模型以为"上一轮没说话"。
        """
        if not (text or "").strip():
            return
        self._push(platform, channel, {"role": "assistant", "content": text})

    def _push(self, platform: str, channel: str, row: dict) -> None:
        key = (platform or "", channel or "")
        with self._lock:
            rows = self._data.setdefault(key, [])
            rows.append(row)

            # 只留最近 max_turns 轮
            limit = self.max_turns * 2
            if len(rows) > limit:
                del rows[: len(rows) - limit]

    # ── 清理 ──

    def forget(self, platform: str, channel: str) -> None:
        """忘掉一个会话。"""
        key = (platform or "", channel or "")
        with self._lock:
            self._data.pop(key, None)

    def stats(self) -> dict:
        """现在记了几个会话、多少条。"""
        with self._lock:
            return {
                "sessions": len(self._data),
                "messages": sum(len(v) for v in self._data.values()),
            }


# 进程内共享一份
_MEMORY = Memory()


def shared() -> Memory:
    """拿全局的那一份。"""
    return _MEMORY
