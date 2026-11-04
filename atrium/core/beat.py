"""心跳 —— 让外面的人知道"atrium 还活着"。

## 为什么需要

    atrium 不监听端口（它是主动连平台出去的）。
    所以外面没法用"端口通不通"判断它活没活。

    而看门狗要判断"该不该重启它"，就得有个活的证据。

## 怎么做

    起来之后，每 10 秒往 ~/.atrium/alive.json 写一次时间戳。

    看门狗读这个文件：
        超过 60 秒没更新 → 判定挂了

## ⚠️ 为什么写文件而不是开个 socket

    一、不占端口。atrium 可能跑在别人的机器上，端口不一定空着。
    二、好观察。用户想知道"它活没活"，cat 一下就行。
    三、进程崩溃后文件还在，能看出"最后一次活着是什么时候"。

## ⚠️ 为什么要带进程号

    同一个机器上可能跑过好几个 atrium 实例（调试时忘了关）。
    带着 pid，看门狗能分清"这个心跳是哪个进程写的"，
    也能顺手把僵死的旧进程清掉。
"""

from __future__ import annotations

import json
import os
import threading
import time

#: 多久写一次
INTERVAL = 10.0


def path() -> str:
    """心跳文件在哪。"""
    return os.path.join(os.path.expanduser("~"), ".atrium", "alive.json")


class Beat:
    """心跳线程。"""

    def __init__(self, interval: float = INTERVAL) -> None:
        self.interval = max(1.0, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 启动时间：用来算"活了多久"
        self.started = time.time()

    def start(self) -> None:
        if self._thread is not None:
            return

        def _loop() -> None:
            while not self._stop.is_set():
                self._write()
                self._stop.wait(self.interval)

        # ⚠️ 先写一次再起线程 —— 别让看门狗有一秒钟的窗口以为它没起来。
        self._write()
        self._thread = threading.Thread(target=_loop, name="beat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # 停的时候把文件删掉，表示"主动退出，不是崩了"
        try:
            os.remove(path())
        except OSError:
            pass

    def _write(self) -> None:
        d = {
            "pid": os.getpid(),
            "ts": time.time(),
            "started": self.started,
            "uptime": round(time.time() - self.started, 1),
        }
        p = path()
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, p)
        except OSError:
            # 写不了心跳不该让 atrium 挂掉
            pass


def read() -> dict | None:
    """读心跳。没有就返回 None。"""
    try:
        with open(path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def age() -> float | None:
    """心跳多久没更新了（秒）。返回 None 表示没有心跳文件。"""
    d = read()
    if not d:
        return None
    try:
        return max(0.0, time.time() - float(d.get("ts") or 0))
    except (TypeError, ValueError):
        return None
