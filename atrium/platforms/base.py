"""平台接口 —— 每个平台实现它。

## 为什么要有这层

    总线不认识飞书、扣子、QQ。
    它只认识 `Platform` 这个接口。

    加一个平台 = 写一个类，实现四个方法。

## ⚠️ 为什么 start 要能一直阻塞

    飞书用 WebSocket 收消息，扣子也是。
    它们都是"连上之后一直挂着"。

    start() 的设计是：
        · 连上
        · 循环读消息
        · 每读一条 → 调 on_message(Message)
        · stop() 被调用时优雅退出

    而 run_forever() 是这个循环本身。

## ⚠️ 为什么 send 要能失败

    平台会限流、会断线、会超时。
    send() 返回 True/False，不抛异常 ——
    失败的消息交给上层重试，不是崩溃。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from atrium.core.message import Message, Reply


class PlatformError(Exception):
    """平台层的错。

    ⚠️ 跟业务错分开。
       平台断线要重连，业务错要告诉用户 —— 处理方式完全不同。
    """


class Platform(ABC):
    """所有平台的父类。"""

    #: 平台名（feishu / coze / qq / ...）
    name: str = ""

    def __init__(self) -> None:
        self._on_message: Callable[[Message], None] | None = None
        self._running = False

    # ══════════════════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════════════════

    @abstractmethod
    def start(self, on_message: Callable[[Message], None]) -> None:
        """连上平台，开始收消息。

        on_message 每收到一条就调一次。

        ⚠️ 这个方法是**阻塞的** —— 它一直挂着。
           要停就调 stop()。
        """

    def stop(self) -> None:
        """停下来。

        默认实现只是把 _running 置 False。
        需要清理资源的（关 WebSocket 之类）要覆盖它。
        """
        self._running = False

    # ══════════════════════════════════════════════════
    # 发消息
    # ══════════════════════════════════════════════════

    @abstractmethod
    def send(self, reply: Reply) -> bool:
        """发一条消息回去。

        返回 True 表示发出去了。
        失败返回 False（不抛异常）—— 上层会重试。
        """

    # ══════════════════════════════════════════════════
    # 辅助
    # ══════════════════════════════════════════════════

    def _emit(self, msg: Message) -> None:
        """收到一条消息，交给上层。"""
        if self._on_message is not None:
            try:
                self._on_message(msg)
            except Exception:  # noqa: BLE001
                # ⚠️ 上层处理出错不能把接收循环打挂。
                #    一条消息处理失败，下一条还得能进来。
                import traceback

                traceback.print_exc()

    def health(self) -> dict:
        """健康状况。界面上显示用。"""
        return {
            "name": self.name,
            "running": self._running,
        }
