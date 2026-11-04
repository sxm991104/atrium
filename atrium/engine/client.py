"""引擎客户端 —— 调后面那个真正干活的东西。

## 为什么要单独一层

    atrium 不该写死"后面是 remote-dispatch"。

    可能的情况：
        · remote-dispatch 的 HTTP 接口（8790）
        · remote-dispatch 的命令行（core/bridge.py）
        · 将来换别的引擎

    所以中间放一层：引擎只暴露两个动作 ——
        submit(task)    派一个活，立刻返回
        poll(id)        问一个活怎么样了

    atrium 只认识这两个。

## ⚠️ 为什么是 submit/poll 而不是同步 wait

    平台会超时（飞书 3 秒）。
    同步等引擎跑完再回，用户看到的是"发送失败"。

    所以：
        submit  → 立刻返回一个 id
        平台回 "已派出"
        后台 poll，跑完了通过 outbox 推回去
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from abc import ABC, abstractmethod


class EngineError(Exception):
    """引擎层的错。"""


class Engine(ABC):
    """引擎接口。"""

    @abstractmethod
    def submit(self, task: str, name: str = "", profile: str = "") -> dict:
        """派一个活。

        返回 {"ok": bool, "id": str, "error": str}
        """

    @abstractmethod
    def poll(self, name: str) -> dict:
        """问一个活怎么样了。

        返回 {"status": "...", "result": "...", "error": "..."}
        """

    @abstractmethod
    def list_tasks(self, limit: int = 20) -> list[dict]:
        """列出任务。"""


# ══════════════════════════════════════════════════════
# remote-dispatch 的命令行实现
# ══════════════════════════════════════════════════════

class RemoteDispatch(Engine):
    """调 remote-dispatch 的 core/bridge.py。

    ⚠️ 为什么走命令行不走 HTTP：
        命令行是单次调用，不依赖那个服务跑着。
        8790 那个服务在桌面端关闭后可能没起来，
        但任务派发必须一直能用。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.bridge = os.path.join(path, "core", "bridge.py")
        if not os.path.exists(self.bridge):
            raise EngineError(f"找不到 {self.bridge}")

    def _call(self, args: list[str], timeout: int = 60):
        """调一次 bridge，返回它打印的 JSON。"""
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        try:
            r = subprocess.run(
                [sys.executable, self.bridge, *args],
                capture_output=True,
                timeout=timeout,
                cwd=self.path,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise EngineError("引擎超时") from None
        except OSError as e:
            raise EngineError(str(e)) from e

        out = (r.stdout or b"").decode("utf-8", "replace").strip()
        err = (r.stderr or b"").decode("utf-8", "replace").strip()

        # 取最后一行 JSON
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith("{") or line.startswith("["):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue

        raise EngineError((err or out or "引擎没有输出")[:400])

    def submit(self, task: str, name: str = "", profile: str = "") -> dict:
        task = (task or "").strip()
        if not task:
            return {"ok": False, "id": "", "error": "任务是空的"}

        nm = (name or "").strip() or ("任务" + str(abs(hash(task)) % 10000))

        args = ["dispatch", task, "--id", nm]
        if profile:
            args += ["--profile", profile]

        try:
            r = self._call(args, timeout=30)
        except EngineError as e:
            return {"ok": False, "id": nm, "error": str(e)}

        if not isinstance(r, dict):
            return {"ok": False, "id": nm, "error": "引擎返回看不懂"}

        if r.get("status") == "error":
            return {"ok": False, "id": nm, "error": r.get("error") or ""}

        # ⚠️ 被闸门拦下时，status 是 "blocked"，不是 "error"。
        #
        #    以前只判 "error"，于是被拦的时候以为派成功了 ——
        #    前台跟用户说"已经派下去了"，其实任务压根没进队列。
        #
        #    实测（2026-11-04）：任务端锁死后派活，
        #    前台说派出去了，去查是 not-found。
        if r.get("status") == "blocked":
            why = (r.get("guard_reason") or r.get("reason")
                   or r.get("error") or "被安全闸拦下了")
            return {
                "ok": False,
                "id": nm,
                "error": str(why),
                "blocked": True,
            }

        return {"ok": True, "id": nm, "error": ""}

    def poll(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            return {"status": "error", "error": "要查哪个任务"}

        try:
            r = self._call(["stats", "--id", name], timeout=20)
        except EngineError as e:
            return {"status": "error", "error": str(e)}

        if not isinstance(r, dict):
            return {"status": "error", "error": "引擎返回看不懂"}

        return r

    def list_tasks(self, limit: int = 20) -> list[dict]:
        try:
            r = self._call(["list", "--limit", str(int(limit or 20))], timeout=20)
        except EngineError:
            return []

        if isinstance(r, list):
            return [x for x in r if isinstance(x, dict)]
        if isinstance(r, dict):
            items = r.get("tasks") or r.get("items") or []
            if isinstance(items, list):
                return [x for x in items if isinstance(x, dict)]
        return []

    # ── 下面三个是 remote-dispatch 特有的 ──
    #
    # ⚠️ remote-dispatch 的 dispatch 只写队列，**不会自己跑**。
    #    必须另外起一个 `bridge.py run --id <id>` 进程。
    #
    #    不补这一步的后果（实测 2026-11-04）：
    #        任务派下去，状态永远 pending，
    #        25 秒后执行器还在等，最后超时。

    def spawn_worker(self, task_id: str) -> bool:
        """起一个 worker 去跑这个任务。

        返回 True 表示起起来了。
        """
        if not task_id:
            return False

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        # Windows 上别弹黑框
        flags = 0x08000000 if sys.platform == "win32" else 0

        try:
            subprocess.Popen(
                [sys.executable, self.bridge, "run", "--id", task_id],
                cwd=self.path,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=flags,
            )
            return True
        except OSError as e:
            print(f"[engine] 起 worker 失败：{e}", file=sys.stderr)
            return False

    def cancel(self, task_id: str) -> bool:
        """停一个任务。"""
        try:
            r = self._call(["cancel", "--id", task_id], timeout=20)
        except EngineError:
            return False
        return not (isinstance(r, dict) and r.get("status") == "error")

    def live_detail(self, task_id: str) -> dict:
        """此刻在干什么（只有 remote-dispatch 支持得比较好）。"""
        try:
            live = self._call(["live"], timeout=20)
        except EngineError:
            return {}

        if not isinstance(live, dict):
            return {}

        prog = live.get("progress") or {}
        if isinstance(prog, dict) and prog.get("id") == task_id:
            return prog

        for t in (live.get("running_tasks") or []):
            if isinstance(t, dict) and t.get("id") == task_id:
                return t

        return {}


def make_engine(cfg: dict) -> Engine:
    """按配置造一个引擎。"""
    typ = (cfg or {}).get("type") or "remote-dispatch"
    path = (cfg or {}).get("path") or ""

    if typ == "remote-dispatch":
        if not path:
            raise EngineError("配置里缺 engine.path")
        return RemoteDispatch(path)

    raise EngineError(f"不认识的引擎类型：{typ}")
