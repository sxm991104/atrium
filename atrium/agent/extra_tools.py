"""补上漏搬的三个工具。

## 为什么单独一个文件

    tools.py 已经五百多行了，而且这四个是"查状态 / 续话"这一类，
    跟派活那套不太一样。

    ⚠️ 更重要：它们是拆分支时漏掉的，放一起好对账，
       下次有人问"还有没有漏的"，看这个文件的 docstring 就行。

## 三个

    continue_task    往已有窗口追一句话（用户说「继续」时必须用它）
    list_models      有哪些模型可用，叫什么叫（别让模型瞎猜）
    engine_status    任务端整体状态（锁没锁、能不能派、几条在跑）

## ⚠️ 为什么不并进 task_stats

    task_stats 查的是**一个任务**。
    这三个查的是**整台机器**或者**配置**。
    混在一起前台会分不清该传什么参数。
"""

from __future__ import annotations

from atrium.engine import api as _api


def continue_task(name: str = "", message: str = "") -> str:
    """往一个已有的任务窗口里接着说一句话，不新开窗口。

    ## 什么时候用它（别调 dispatch_task）

        · 你刚问过「继续还是新开」，用户回「继续」/「接着做」/「对」
        · 用户在同一件事上追加要求（「再把颜色改一下」）
        · 任务还没做完，用户想补充说明

    ## ⚠️ 「继续」必须走这个，不能重派新任务

        重派会被当成又开一件新活：
            可能又触发一遍成本检查、
            又弹一遍同一个问题、来回打转。

        实测卡死过整条链路 ——
        任务永远派不出去，全卡在问候语上。

        一句话区分：
            新的一件事    → dispatch_task
            还在同一件事里 → continue_task
    """
    name = (name or "").strip()
    msg = (message or "").strip() or "继续"

    if not name:
        return "要往哪个任务里说？告诉我任务名。"

    try:
        d = _api.call("/api/continue", {"name": name, "message": msg})
    except _api.ApiError as e:
        return f"没送进去：{e}"

    if d.get("error"):
        return f"没送进去：{d.get('error')}"

    status = str(d.get("status") or "")

    if status == "blocked":
        # ⚠️ 被闸门拦下了 —— 这不是失败，是要用户确认
        return (
            f"「{name}」被拦下来等确认了。\n"
            f"原因：{d.get('guard_reason') or d.get('reason') or '看着像危险操作'}\n\n"
            "用户要是坚持，我再走一遍确认流程。"
        )

    return f"话送进「{name}」了，它接着往下做。跑完我告诉你。"


def list_models() -> str:
    """看看电脑上启用了哪几套模型，各自什么情况。

    ## 什么时候用

        · 用户说「用便宜的做」「派给强一点的模型」—— 你需要知道有哪些
        · 用户问「现在能用哪些模型」
        · 派活前想确认某个名字对不对

    ## ⚠️ 别瞎猜模型名

        不要凭空写「kimi」这种。先调这个拿到真实的名字，
        再拿那个名字去 dispatch_task 的 model 参数。
    """
    try:
        d = _api.call("/api/engine/profiles")
    except _api.ApiError as e:
        return f"查模型配置失败：{e}"

    profiles = d.get("profiles") or []
    active = str(d.get("active") or "")

    if not profiles:
        return "一套模型都没配。让用户去桌面端「设置 → 模型」填一下。"

    usable = []
    for p in profiles:
        if not isinstance(p, dict):
            continue
        if not p.get("enabled"):
            continue
        usable.append(p)

    if not usable:
        return "配了模型但都停用了。让用户去桌面端开一个。"

    lines = [f"能用 {len(usable)} 套模型："]

    for p in usable:
        name = str(p.get("name") or p.get("id") or "?")
        model = str(p.get("model") or "")
        conc = p.get("concurrency") or 3
        tail = " ★当前默认" if str(p.get("id")) == active else ""
        lines.append(f"  · {name}  —— {model}（最多同时跑 {conc} 个）{tail}")

    lines.append("")
    lines.append("派活时说「派给 xxx」，model 参数就填上面那个名字。")

    return "\n".join(lines)


def check_asks(limit: int = 5) -> str:
    """看电脑端卡住时推回来的问题。

    ## 什么时候用

        用户问「它怎么不动了」「是不是卡住了」
        任务状态一直是 running 但没进展

    ## 这个跟别的有什么不同

        task_stats 告诉你"状态是 running"，
        但这个工具告诉你"**它在等你回答一个问题**"。

        任务跑到一半可能需要用户给个信息
        （某个文件在哪、要哪种格式），
        拿不到就往 ask.jsonl 写一行，然后停在那儿。

        光看 task_stats 会以为它在正常跑，
        其实早就在等人了。
    """
    try:
        n = int(limit or 5)
    except (TypeError, ValueError):
        n = 5

    try:
        d = _api.call(f"/api/asks?limit={n}")
    except _api.ApiError as e:
        return f"查不到：{e}"

    asks = d.get("asks") or []

    if not asks:
        return "现在没有任务卡在等回答。"

    lines = [f"有 {len(asks)} 个问题在等你回答："]

    for i, a in enumerate(asks, 1):
        if not isinstance(a, dict):
            lines.append(f"  {i}. {str(a)[:100]}")
            continue

        # ⚠️ 字段名不统一：正常写的是 need，
        #    写坏了的是 raw（原文保留）。
        need = str(a.get("need") or a.get("raw") or "").strip()
        who = str(a.get("id") or a.get("task") or "").strip()

        head = f"  {i}. "
        if who:
            head += f"「{who}」—— "
        head += need[:160] or "（内容为空）"
        lines.append(head)

    lines.append("")
    lines.append("这些是电脑端拿不到东西时问的。回答之后它会接着做。")

    return "\n".join(lines)


def engine_status() -> str:
    """看一眼任务端现在的状态：锁没锁、能不能派。

    ## 什么时候用（重要）

        用户问「现在能派吗」「为什么派不出去」「是不是卡住了」
        这类问题时，**先调这个**，不要凭聊天上下文里的印象回答。

    ## 为什么必须查

        任务端会被锁（被拦满三次）、会排队、会卡住。
        这些状态变化跟聊天内容无关 ——
        聊得再顺，任务端也可能是锁着的。

        实测：锁死之后问「现在能派吗」，
        前台凭印象说"能的"，结果派什么都被拒。
    """
    lines = []

    # ── 一、闸门 / 围栏 ──
    try:
        g = _api.call("/api/guard")
        if g.get("enabled"):
            lines.append(f"验证码闸门：开（码发到 {g.get('email_masked') or '?'}）")
        else:
            lines.append("验证码闸门：关（危险操作不拦）")
    except _api.ApiError as e:
        lines.append(f"验证码闸门：查不到（{e}）")

    # ── 二、服务在不在 ──
    try:
        s = _api.call("/api/services")
        on = []
        for k, label in (("bridge", "桥上服务"), ("feishu", "飞书"),
                         ("qq", "QQ")):
            if s.get(k):
                on.append(label)
        lines.append("服务：" + ("、".join(on) if on else "都没起"))
    except _api.ApiError:
        pass

    # ── 三、有没有任务卡在等用户回答 ──
    #
    # ⚠️ 这个还没接上（引擎没开 HTTP 接口），先不编。
    #    等补上了再往里加，现在写了就是骗人。

    return "\n".join(lines)
