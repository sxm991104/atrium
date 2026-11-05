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

    实测（2026-11-04）：派了 4 个活全卡在 pending 不动 ——
    因为只写了队列，没人起 worker 去跑。
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
    # ── 下面是验证码 / 提权 / 解锁 ──
    #
    # ⚠️ 下面五个是拆分支那会儿漏掉的。
    #
    #    手册（prompt.py）里写着"提权要验证码、你不是发码的人"，
    #    但工具清单只有上面四个，前台根本没有收码的手段 ——
    #    手册会说，手上没有。
    #
    #    实测（2026-11-04）：老爸翻 front.py 发现了这个缺口。
    #
    #    还有一个更隐蔽的：**发码本身**也没有工具。
    #    用户碰到权限墙，引擎只回"你被拦了"，
    #    没有任何人去发那个码 —— 用户永远收不到。
    #    所以补了 confirm_dangerous_action（三级确认，第 3 级才发码）。
    {
        "type": "function",
        "function": {
            "name": "confirm_dangerous_action",
            "description": (
                "危险操作的三级确认。**第 3 级才真的发验证码到用户邮箱。**\n"
                "第 1 次提醒（level=1）、第 2 次警告（level=2）、"
                "第 3 次发码（level=3）。\n"
                "用户说了危险操作（删库、格式化、往系统目录写东西）时用这个。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "用户要做的危险操作，用他的原话",
                    },
                    "level": {
                        "type": "integer",
                        "description": (
                            "第几次。1=提醒（不发码），2=警告（不发码），"
                            "3=发验证码"
                        ),
                    },
                    "purpose": {
                        "type": "string",
                        "description": "为什么要做这个（可选）",
                    },
                },
                "required": ["task", "level"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_task_code",
            "description": (
                "把用户报过来的验证码交给引擎校验（针对**某一件具体的活**）。"
                "用户说「邮箱里收到码了」「码是 123456」时用这个。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "哪个窗口要验证（任务名）",
                    },
                    "code": {
                        "type": "string",
                        "description": "用户报的验证码",
                    },
                },
                "required": ["task", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unlock_all",
            "description": (
                "解锁**整个任务端**（不针对哪件活）。"
                "用户说「整个电脑锁死了」「什么都做不了了」时用这个。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "用户报的验证码",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_permission",
            "description": (
                "查或改某个窗口的权限档。"
                "用户说「让它能碰桌面」「这个任务权限太小」时用这个。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "窗口名。留空 = 列出所有窗口的权限",
                    },
                    "level": {
                        "type": "string",
                        "description": (
                            "workspace（只能动任务目录）/ "
                            "home（能动用户目录）/ full（全部）。"
                            "留空 = 只查不改"
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "guard_status",
            "description": (
                "看验证码那块配好没有。"
                "用户说「码没收到」「验证码是不是坏了」时先看这个。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    # ── 静默模式 ──
    #
    # ⚠️ 这是"验过码之后把验证关卡关一段时间"，不是"不验就关"。
    #    关掉的东西包括：动作守卫 / 提权 / 危险操作 / 审计员 / 外传。
    #    所以开它本身必须先验码 —— 见 quiet_mode 的 docstring。
    {
        "type": "function",
        "function": {
            "name": "quiet_mode",
            "description": (
                "静默模式：验过码之后，把所有要验证码的关卡"
                "（动作守卫、提权、危险操作、审计员、外传）关一段时间。\n"
                "用户说「关到下午五点」「关两个小时」「别再弹码了」时用。\n"
                "⚠️ 开静默**必须先验码**；关多久**由用户说**，不要自己定。\n"
                "⚠️ 用户只说了「关了吧」没说多久 → 先问他要关到什么时候。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "用户报的验证码（开静默必填）",
                    },
                    "seconds": {
                        "type": "integer",
                        "description": (
                            "关多久（秒）。用户说「到下午五点」你自己算出现在到那时多少秒"
                        ),
                    },
                    "stop": {
                        "type": "boolean",
                        "description": "True = 立刻结束静默（这个不用码）",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "quiet_status",
            "description": "静默模式现在开着吗、还剩多久。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    # ── 续话 / 查模型 / 查状态 ──
    #
    # ⚠️ 这三个也是拆分支时漏的。
    #
    #    continue_task 尤其要紧：
    #    用户说「继续」时如果重派新任务，
    #    会被当成又开一件新活，来回打转，实测卡死过整条链路。
    {
        "type": "function",
        "function": {
            "name": "continue_task",
            "description": (
                "往一个已有的任务窗口里接着说一句话，**不新开窗口**。\n"
                "用户说「继续」「接着做」「再改一下」时用这个，"
                "**不要重新 dispatch_task**。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "往哪个任务窗口里说",
                    },
                    "message": {
                        "type": "string",
                        "description": "要说的这句话。可以很短，比如「继续」",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_models",
            "description": (
                "看有哪些模型可用、名字叫什么。"
                "**派活前想指定模型时先调这个**，别瞎猜名字。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_asks",
            "description": (
                "看电脑端卡住时推回来的问题。\n"
                "任务状态一直是 running 但没进展、"
                "或者用户问「它怎么不动了」时用这个。\n"
                "跟 engine_status 的区别：那个看整机状态，"
                "这个看「有哪些任务在等用户回答」。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "最多看几条，默认 5",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "engine_status",
            "description": (
                "看任务端整体状态（锁没锁、服务起没起）。"
                "用户问「现在能派吗」「为什么派不出去」「是不是卡住了」时先调这个。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    # ── 安全模式 ──
    #
    # ⚠️ 为什么前台要有这个工具
    #
    #    「出事可回退」这个模式要开系统卷影副本保护 ——
    #    而远程办公时用户不在电脑前，没法点桌面端的按钮。
    #
    #    但前台是暴露在外的：消息可能来自飞书/钉钉，
    #    可能是别人借手机发的。所以这个工具比桌面端严得多：
    #        · **一律要验证码**（不填先发一封到他邮箱）
    #        · **只能往保护多的方向切，不能往少的方向切**
    #
    #    （2026-11-05 老爸定的：
    #       「要那个码，到前台了都要码宝贝，
    #         因为前台是暴露在外的，桌面端是用户电脑。」
    #       「前台只能去开，不能关。」）
    {
        "type": "function",
        "function": {
            "name": "security_mode",
            "description": (
                "查看或切换安全模式。\n"
                "用户说「保护严格点」「开回退」「换个安全模式」时用这个。\n"
                "\n"
                "⚠️ 规矩：\n"
                "  · 留空 mode = 只查当前模式和哪些能切\n"
                "  · 要切 **一律要验证码** —— 不填 code 会先发一封到他邮箱，\n"
                "    然后你把「码发到邮箱了，告诉我」回给用户\n"
                "  · **只能往保护多的方向切，不能往少的方向切** ——\n"
                "    想变松的话只能让他自己在电脑上点，你不能代劳"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "description": (
                            "strict（严格防护）/ both（全开）/ rollback（出事可回退）。"
                            "留空 = 只查不改"
                        ),
                    },
                    "code": {
                        "type": "string",
                        "description": (
                            "用户报过来的验证码。"
                            "第一次调不要填（会发码），用户报了码再填上重调一次"
                        ),
                    },
                },
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
        err = str(res.get("error") or "未知错误")

        # ⚠️ 被安全闸拦下的，不是普通失败。
        #
        #    要告诉前台**下一步该干什么** ——
        #    让用户去邮箱拿码，前台调 unlock_all 提交。
        #
        #    只说"派活失败"的话，前台会以为系统坏了，
        #    或者傻等着，不知道该怎么办。
        if res.get("blocked"):
            return (
                "派不出去 —— 被安全闸拦住了：\n"
                f"{err}\n\n"
                "接下来：让用户去邮箱拿验证码，拿到之后告诉我，\n"
                "我用 unlock_all 提交。"
            )

        return f"派活失败：{err}"

    task_id = res.get("id") or name or ""

    # 记一笔，主程序要拿这个去盯进度
    _remember(task_id)

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


# ══════════════════════════════════════════════════════
# 记录：这一轮派出了哪些任务
# ══════════════════════════════════════════════════════
#
# ⚠️ 为什么要有这个。
#
#    前台调 dispatch_task 派活，但它只是"返回一段文字"。
#    主程序（app.py）拿到的只是"前台说的话"，
#    不知道背后派了哪些任务 —— 也就没法盯着它们跑完。
#
#    实测（2026-11-04）：
#        用户派了 10 个任务，前台说"都派出去了"，
#        但没人知道是哪 10 个，跑完了也推不回来。
#
#    所以这里记一笔。主程序每轮开始前清空，轮结束读走。

_DISPATCHED: list[str] = []


def take_dispatched() -> list[str]:
    """取走这一轮派出的任务名，并清空。"""
    global _DISPATCHED

    out = list(_DISPATCHED)
    _DISPATCHED = []
    return out


def _remember(task_id: str) -> None:
    if task_id and task_id not in _DISPATCHED:
        _DISPATCHED.append(task_id)


def _profile_name(pid: str) -> str:
    """把模型内部 id 转成人看的名字。

    ⚠️ 为什么要转：
        remote-dispatch 的 profile 有两种形态：
            "p1793460955495"   内部 id（用户看不懂）
            "deepseek"         名字

        实测（2026-11-04）：
            表格里出现 p1793460955495，用户问"这是啥"。
    """
    pid = (pid or "").strip()
    if not pid:
        return ""

    # 不是内部 id 就直接用
    if not (pid.startswith("p") and pid[1:].isdigit()):
        return pid[0].upper() + pid[1:] if pid.isascii() else pid

    try:
        import json as _json
        import os as _os

        cfg = _os.path.join(
            _os.environ.get("APPDATA") or "", "dsh-bridge", "engine.json"
        )
        with open(cfg, encoding="utf-8") as f:
            d = _json.load(f)

        items = d.get("profiles") or []
        if isinstance(items, dict):
            items = list(items.values())

        for x in items:
            if isinstance(x, dict) and x.get("id") == pid:
                name = str(x.get("name") or "").strip()
                if name:
                    return name
    except (OSError, ValueError):
        pass

    return "默认"


def _scan_outputs(session_id: str, limit: int = 8) -> list[str]:
    """扫工作目录里产出的文件。

    ## 为什么要这个

        引擎的 result 是**一段自然语言**：
            "已在你当前的工作区建好 .text 文件..."

        用户想知道"到底产出了什么"，
        光看文字不够 —— 要到盘上看。

    ## 扫什么

        工作目录下**最新的几个文件**（按修改时间倒序）。

        ⚠️ 跳过隐藏文件、__pycache__ 之类。
           那些不是"产出"。

    ## 返回

        文件名的列表，最多 limit 个。
        目录里的东西不算 —— 用户关心的是文件。
    """
    import os as _os

    if not session_id:
        return []

    try:
        if not _os.path.isdir(session_id):
            return []

        rows = []
        for name in _os.listdir(session_id):
            if name.startswith(".") or name == "__pycache__":
                continue
            full = _os.path.join(session_id, name)
            try:
                st = _os.stat(full)
            except OSError:
                continue
            if not _os.path.isfile(full):
                continue
            rows.append((st.st_mtime, name, st.st_size))

        rows.sort(reverse=True)
        out = []
        for _mt, name, size in rows[:limit]:
            if size < 1024:
                out.append(f"{name} ({size}B)")
            elif size < 1024 * 1024:
                out.append(f"{name} ({size // 1024}KB)")
            else:
                out.append(f"{name} ({size // 1024 // 1024}MB)")
        return out
    except OSError:
        return []


def task_stats(engine: Engine, name: str = "") -> str:
    """查进度。

    ⚠️ 只调引擎的 poll 可能不够 —— 它可能只回状态，
       没有"此刻在干什么"。所以引擎要是能提供更细的就带上。
    """
    name = (name or "").strip()
    if not name:
        return "要查哪个任务？告诉我名字。"

    # ⚠️ 先查 engine 是不是 None。
    #
    #    实测（2026-11-05）：engine=None 时直接
    #        AttributeError: 'NoneType' object has no attribute 'poll'
    #    用户看到的是「执行出错：AttributeError...」——
    #    他不知道这是"引擎没接上"。
    #
    #    这种情况发生在：单独调工具（测试、脚本）、
    #    或者引擎构造失败但前台还在跑。
    if engine is None:
        return (
            "现在没接上任务端 —— 查不了进度。\n"
            "去桌面端看一眼「诊断」页，或者稍等它重连。"
        )

    try:
        r = engine.poll(name)
    except EngineError as e:
        return f"查不到：{e}"
    except AttributeError as e:
        # 引擎对象在，但没有 poll（换了实现、或者没接对）
        return f"这个引擎不支持查进度（{e}）。"

    if not isinstance(r, dict):
        return f"查不到「{name}」的状态。"

    st = r.get("status") or "未知"
    result = (r.get("result") or "").strip()
    err = (r.get("error") or "").strip()
    turns = r.get("turns") or 0
    task_text = (r.get("task") or "").strip()
    profile = (r.get("profile") or "").strip()
    session_id = (r.get("session_id") or "").strip()
    secs = r.get("seconds")

    # ⚠️ 把结构化的字段都摆出来。
    #
    #    以前只给一行 "「名字」：状态"，
    #    用户想知道"用哪个模型跑的""产出什么文件"都看不到。
    #
    #    实测（2026-11-04）：用户要求"按模型分组，列任务名和产出"，
    #    那时候这些字段都没暴露出来。
    lines = [f"任务名：{name}"]

    if profile:
        lines.append(f"模型：{_profile_name(profile)}")
    lines.append(f"状态：{st}")
    if task_text:
        lines.append(f"内容：{task_text[:100]}")
    if secs is not None:
        try:
            lines.append(f"耗时：{float(secs):.1f} 秒")
        except (TypeError, ValueError):
            pass

    # 产出文件
    outs = _scan_outputs(session_id)
    if outs:
        lines.append(f"产出文件（{len(outs)} 个）：")
        for o in outs:
            lines.append(f"  · {o}")

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

    # ── 续话 / 查模型 / 查状态 ──
    #
    # ⚠️ 这三个也走 HTTP，不走命令行：
    #    continue 要调 /api/continue（命令行没有这个命令），
    #    模型配置和状态只有桌面端那个服务知道。
    if name in ("continue_task", "list_models", "engine_status", "check_asks"):
        try:
            from atrium.agent import extra_tools as _et
        except ImportError as e:
            return f"工具载不进来：{e}"

        if name == "continue_task":
            return _et.continue_task(
                args.get("name") or "",
                args.get("message") or "",
            )
        if name == "list_models":
            return _et.list_models()
        if name == "check_asks":
            try:
                n = int(args.get("limit") or 5)
            except (TypeError, ValueError):
                n = 5
            return _et.check_asks(n)
        return _et.engine_status()

    # ── 验证码 / 提权 / 解锁 ──
    #
    # ⚠️ 这几个不走 engine 参数 —— 它们走 HTTP（见 guard_tools.py）。
    #
    #    因为状态在引擎进程里（出码、校验、放行的流程），
    #    命令行那边没暴露这些接口。
    if name in ("confirm_dangerous_action", "verify_task_code",
                "unlock_all", "task_permission", "guard_status",
                "quiet_mode", "quiet_status"):
        try:
            from atrium.agent import guard_tools as _gt
        except ImportError as e:
            return f"验证码工具载不进来：{e}"

        if name == "confirm_dangerous_action":
            try:
                lv = int(args.get("level") or 1)
            except (TypeError, ValueError):
                lv = 1
            return _gt.confirm_dangerous_action(
                args.get("task") or "",
                lv,
                args.get("purpose") or "",
            )
        if name == "verify_task_code":
            return _gt.verify_task_code(
                args.get("task") or "",
                args.get("code") or "",
            )
        if name == "unlock_all":
            return _gt.unlock_all(args.get("code") or "")
        if name == "task_permission":
            return _gt.task_permission(
                args.get("name") or "",
                args.get("level") or "",
                # ⚠️ 第三个参数是验证码。
                #    漏传它 → 升权永远验不过：
                #    用户明明报了码，引擎还是回"要验证码"。
                #
                #    实测（2026-11-04）：就是漏了这个，
                #    端到端测升权一直失败。
                args.get("code") or "",
            )
        if name == "guard_status":
            return _gt.guard_status()

    # ── 安全模式 ──
    #
    # ⚠️ 也走 HTTP，而且是走**要码那条路**（/api/security/mode/verified）。
    #
    #    桌面端用的是不要码的 /api/security/mode ——
    #    用户坐在电脑前，自己看着屏幕点。
    #    前台不一样，一刀切要码。
    #    （2026-11-05 老爸定的：「到前台了都要码」。）
    if name == "security_mode":
        try:
            from atrium.agent import security_tools as _st
        except ImportError as e:
            return f"安全模式工具载不进来：{e}"
        return _st.set_security_mode(
            args.get("mode") or "",
            args.get("code") or "",
        )

    return f"没有这个工具：{name}"
