"""前台工具 —— 派活、查进度、列任务、取消。

## 为什么前台不能有 read_file / run_command

    前台是"在聊天窗口里跟你说话的那个"。
    聊天平台的消息**可能被注入**（网页、文档、别人转发的内容）。

    如果前台自己就能跑命令、改文件：
        一次注入 = 直接落地，中间没有任何关卡。

    而任务引擎那边有完整的关卡：
        动作守卫 / 影子执行 / 审计员 / 出口管控 / 版本回退

    所以前台**只派活，不干活**。

## 从哪来

    这套是 remote-dispatch 里 engine/front_tools.py 搬过来的。
    逻辑一样，只是引擎从 subprocess 调 bridge.py 变成了
    通过 Engine 接口 —— 这样换引擎只改一个地方。

## ⚠️ 为什么 dispatch 之后要起 worker

    只写任务进队列，不会有人去跑 —— 任务会永远卡在 pending。

    实测（2026-11-04）：扣子里派了 4 个活全卡住，
    就是因为没起 worker。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from atrium.engine.client import Engine, EngineError


# ══════════════════════════════════════════════════════
# 工具声明（给模型的 JSON schema）
# ══════════════════════════════════════════════════════

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "dispatch_task",
            "description": (
                "把一件说清楚的事派给电脑端。派完立刻返回，不等结果。"
                "task 必须是用户的原话，一个字都不改。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "用户原话，不用润色",
                    },
                    "task_name": {
                        "type": "string",
                        "description": (
                            "任务叫什么名字（两到六个字，像「登录页」）。"
                            "如果用户说的是一个已有的窗口名，"
                            "那就是往那个窗口续，不是开新窗。"
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "用哪套模型配置。用户说了具体哪套"
                            "（「派给 Kimi」「用 DeepSeek」）就填对应的名字。"
                            "没说就留空。"
                        ),
                    },
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_stats",
            "description": "查一个任务现在到哪一步了、成没成。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "任务名"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": (
                "列出所有任务窗口。"
                "用户说「继续那个」「之前那个」时，先用这个查有哪些。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "最多列几个，默认 20",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_cancel",
            "description": "停掉一个正在跑的任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "任务名"},
                },
                "required": ["name"],
            },
        },
    },
]


# ══════════════════════════════════════════════════════
# 启动 worker
# ══════════════════════════════════════════════════════

# 这个进程里起过的 worker，防止同一个任务起两次
_workers: set[str] = set()


def _spawn_worker(engine: Engine, task_id: str) -> bool:
    """给一个任务起后台 worker。

    ⚠️ 这一步不能省。只派不跑，任务永远 pending。

    ⚠️ 只有 remote-dispatch 需要这个（它的 dispatch 只写队列）。
       别的引擎如果 submit 就直接跑，不用调这个。
    """
    if not task_id:
        return False
    if task_id in _workers:
        return True

    runner = getattr(engine, "spawn_worker", None)
    if runner is None:
        # 这个引擎不需要单独的 worker
        _workers.add(task_id)
        return True

    try:
        ok = bool(runner(task_id))
        if ok:
            _workers.add(task_id)
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"[front] 起 worker 失败：{e}", file=sys.stderr)
        return False


# ══════════════════════════════════════════════════════
# 工具实现
# ══════════════════════════════════════════════════════

def dispatch_task(engine: Engine, task: str = "", task_name: str = "",
                  model: str = "") -> str:
    """派活。"""
    task = (task or "").strip()
    if not task:
        return "任务内容是空的，没法派。"

    name = (task_name or "").strip()

    try:
        res = engine.submit(task, name=name, profile=model)
    except EngineError as e:
        return f"派活失败：{e}"

    if not res.get("ok"):
        return f"派活失败：{res.get('error') or '未知错误'}"

    task_id = res.get("id") or name or ""

    started = _spawn_worker(engine, task_id)

    tail = (
        "跑完会告诉你。想知道进度，过会儿问我。"
        if started
        else "但后台没起起来，这条可能不会自己跑。"
    )

    return (
        f"已经派下去了，窗口叫「{task_id}」。\n"
        f"任务：{task[:80]}\n"
        f"{tail}"
    )


def task_stats(engine: Engine, name: str = "") -> str:
    """查进度。

    ⚠️ 只调引擎的 poll 可能不够 —— 它可能只回状态，
       没有"此刻在干什么"。所以引擎要是能提供更细的就带上。
    """
    name = (name or "").strip()
    if not name:
        return "要查哪个任务？告诉我名字。"

    try:
        r = engine.poll(name)
    except EngineError as e:
        return f"查不到：{e}"

    if not isinstance(r, dict):
        return f"查不到「{name}」的状态。"

    st = r.get("status") or "未知"
    result = (r.get("result") or "").strip()
    err = (r.get("error") or "").strip()
    turns = r.get("turns") or 0
    task_text = (r.get("task") or "").strip()

    lines = [f"「{name}」：{st}"]

    if task_text:
        lines.append(f"任务：{task_text[:100]}")

    # 在跑的，试试拿更细的进度
    if st in ("running", "pending"):
        detail = getattr(engine, "live_detail", None)
        if callable(detail):
            try:
                prog = detail(name) or {}
            except Exception:  # noqa: BLE001
                prog = {}
            if prog:
                phase = prog.get("phase") or ""
                doing = prog.get("doing") or prog.get("last_action") or ""
                el = prog.get("elapsed_seconds")
                if phase:
                    lines.append(f"阶段：{phase}")
                if doing:
                    lines.append(f"此刻在做：{doing}")
                if el is not None:
                    try:
                        lines.append(f"已跑：{int(float(el))} 秒")
                    except (TypeError, ValueError):
                        pass
            else:
                if st == "pending":
                    lines.append("还没开始跑（可能在排队）")

    if turns:
        lines.append(f"已经跑了 {turns} 轮")
    if err:
        lines.append(f"出错：{err[:300]}")
    if result:
        lines.append(f"结果：{result[:400]}")

    return "\n".join(lines)


def task_list(engine: Engine, limit: int = 20) -> str:
    """列任务。"""
    try:
        tasks = engine.list_tasks(int(limit or 20))
    except EngineError as e:
        return f"列不出来：{e}"

    if not tasks:
        return "现在没有任何任务。"

    lines = [f"共 {len(tasks)} 个任务："]
    for t in tasks[: int(limit or 20)]:
        if not isinstance(t, dict):
            continue
        tid = t.get("id") or "?"
        stt = t.get("status") or "?"
        desc = (t.get("task") or "")[:40]
        lines.append(f"  · {tid}  [{stt}]  {desc}")
    return "\n".join(lines)


def task_cancel(engine: Engine, name: str = "") -> str:
    """取消。"""
    name = (name or "").strip()
    if not name:
        return "要停哪个任务？告诉我名字。"

    cancel = getattr(engine, "cancel", None)
    if not callable(cancel):
        return "这个引擎不支持取消任务。"

    try:
        ok = cancel(name)
    except EngineError as e:
        return f"停不下来：{e}"
    return f"「{name}」已经停了。" if ok else f"「{name}」停不下来。"


# ══════════════════════════════════════════════════════
# 分发
# ══════════════════════════════════════════════════════

def execute(engine: Engine, name: str, args: dict) -> str:
    """执行一个前台工具。

    ⚠️ 不认识的名字**必须报错**，不能"随便找个工具跑"。
       前台的工具面很窄，走错一步就是意外。
    """
    if name == "dispatch_task":
        return dispatch_task(
            engine,
            args.get("task") or "",
            args.get("task_name") or "",
            args.get("model") or "",
        )
    if name == "task_stats":
        return task_stats(engine, args.get("name") or "")
    if name == "task_list":
        try:
            n = int(args.get("limit") or 20)
        except (TypeError, ValueError):
            n = 20
        return task_list(engine, n)
    if name == "task_cancel":
        return task_cancel(engine, args.get("name") or "")

    return f"没有这个工具：{name}"
