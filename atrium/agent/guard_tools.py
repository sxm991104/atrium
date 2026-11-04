"""给 atrium 的前台加验证码 / 提权 / 解锁工具。

## 为什么需要

    拆分的时候只搬了四个基础工具（派活、查进度、列任务、取消），
    验证码那一套没搬。

    但 prompt.py 里的派活手册明明写着"提权要验证码"，
    前台照着手册做，却发现自己手上没有收码的工具 ——
    只能靠聊天把码"复述"过去，实际上根本没人校验。

    **手册会说，手上没有工具。这是我的疏漏。**

## 四个新工具

    verify_task_code   「把邮箱里的码填进来」 —— 针对某一件具体的活
    unlock_all         「整个电脑锁死了，解锁」 —— 不针对哪件
    task_permission    查 / 改某个窗口的权限档
    guard_status       验证码那块配好没有（邮箱通不通）

## ⚠️ verify_task_code 和 unlock_all 的区别（手册里也写了）

    verify_task_code   验证**某一件**具体的活能不能做
                       码跟任务名绑定，换个任务就得重新要

    unlock_all         解锁**整个任务端**
                       码记在一个固定目标上，与任务无关

    用户收的码是哪一种，看当时屏幕/邮件里怎么说的。
    拿错了会验不过 —— 不是码错，是类型错，要提示用户分辨。

## ⚠️ 这些走 HTTP，不走命令行

    派活查进度走命令行（不依赖服务在跑）。
    验证码这套走 HTTP（8790）—— 因为状态在引擎进程里，
    命令行没暴露这些接口。

    服务没起来时，这几个工具会明确告诉用户"桌面端没起"，
    而不是报一堆 urllib 的错。
"""

from __future__ import annotations

from atrium.engine import api as _api


def confirm_dangerous_action(task: str = "", level: int = 1,
                             purpose: str = "") -> str:
    """危险操作的三级确认。**第 3 级才发码。**

    ## 为什么不能靠"问一句确认"

        用户说「删库」，你回「确定吗？」，用户回「确定」——
        然后你删了。**这个流程挡不住任何恶意。**

        拿到手机的人能翻聊天记录、能学用户的语气、
        能说一百遍「确认」。对话通道一旦被拿到，
        你说的每一句话都是替贼开门。

        真正独立的通道得是**不在对话里的东西**——邮箱。

    ## 三级怎么走

        第 1 次（level=1）  提醒
            用户说了危险操作。你先正常回一句提醒，
            让他有机会自己停下（说错话、用词夸张、手滑）。

        第 2 次（level=2）  警告
            用户还是坚持。再警告一次，说明后果不可逆。

        第 3 次（level=3）  验证码
            用户第三次坚持 → **这个工具真的发码**。
            然后跟用户说「码发到你邮箱了，告诉我」。

    ## 什么时候算「一次」

        用户说出危险要求 → 你给一次提醒，这算第 1 次。
        下一句用户又说要做 → 第 2 次。
        再说 → 第 3 次，发码。

    ## ⚠️ 发出去的码不会返回给你

        引擎只回"发出去了没有"，不返回码本身。
        码只走邮件这一条路 —— 返回给你就等于写进了对话上下文，
        那这个码就废了。
    """
    task = (task or "").strip()
    if not task:
        return "要确认哪件事？把用户的原话给我。"

    try:
        lv = int(level)
    except (TypeError, ValueError):
        lv = 1

    if lv <= 0:
        lv = 1
    if lv > 3:
        lv = 3

    # ⚠️ 第 1、2 级**不调接口**，只返回话术。
    #
    #    因为这两级只是"提醒用户停下来"，
    #    还没有到要发码的程度。
    #    发码是一件重事：一旦发码，整个任务端会锁住。
    if lv == 1:
        return (
            "【第 1 次提醒】\n\n"
            "这次要动的东西有点重，我先拦一下：\n"
            f"{task[:200]}\n\n"
            "如果是说错了、或者只是随口一说，现在停还来得及。\n"
            "真要做的，再跟我说一遍。"
        )

    if lv == 2:
        return (
            "【第 2 次警告】\n\n"
            "这个操作做下去**收不回来**：\n"
            f"{task[:200]}\n\n"
            "再来一次，我就往你邮箱发验证码了 ——"
            "那个码只有你本人能拿到。"
        )

    # 第 3 级：发码
    try:
        d = _api.call(
            "/api/guard/code",
            {"task": task, "purpose": purpose or ""},
            timeout=40,
        )
    except _api.ApiError as e:
        return f"发码没走通：{e}"

    ok = bool(d.get("ok"))
    msg = str(d.get("message") or d.get("error") or "").strip()

    if not ok:
        return f"❌ 码没发出去：{msg or '未知原因'}"

    return (
        "【第 3 次 · 已发验证码】\n\n"
        f"{msg}\n\n"
        "码到你邮箱了，拿到之后告诉我，我验一下。\n"
        "（注意：你收的是**这件事**的码，跟「整个电脑锁死」那种不是一个。）"
    )


def verify_task_code(task: str = "", code: str = "") -> str:
    """把用户报的验证码交给引擎校验。

    ⚠️ 校验通过之后**不会自动重派**。
       手册里的流程是"提权成功后再派一次"，
       那一步由前台自己用 dispatch_task 做 ——
       因为要不要重派、派给谁，是它该判断的。
    """
    task = (task or "").strip()
    code = (code or "").strip()

    if not task:
        return "要给哪个任务验证？告诉我窗口名。"
    if not code:
        return "验证码是空的。让用户去邮箱里看一下。"

    try:
        d = _api.call("/api/guard/verify", {"task": task, "code": code})
    except _api.ApiError as e:
        return f"校验没走通：{e}"

    ok = bool(d.get("ok"))
    msg = str(d.get("message") or d.get("error") or "").strip()

    if ok:
        return (
            f"✅ 验证通过，「{task}」可以做了。\n"
            f"{msg}\n\n"
            f"现在用同一个窗口名再派一次活就行。"
        )

    return f"❌ 没通过：{msg or '码不对或已过期'}"


def unlock_all(code: str = "") -> str:
    """解锁整个任务端。

    ⚠️ 这是"整个锁死"时用的，不针对哪件活。
       用户收的是任务验证码时不要调这个 —— 会验不过。
    """
    code = (code or "").strip()
    if not code:
        return "验证码是空的。让用户去邮箱里看一下。"

    try:
        d = _api.call("/api/guard/unlock", {"code": code})
    except _api.ApiError as e:
        return f"解锁没走通：{e}"

    ok = bool(d.get("ok"))
    msg = str(d.get("message") or d.get("error") or "").strip()

    if ok:
        return f"✅ 解锁了。\n{msg}"
    return f"❌ 没解锁：{msg or '码不对或已过期'}"


def task_permission(name: str = "", level: str = "",
                    code: str = "") -> str:
    """查 / 改某个窗口的权限档。

    level 留空 = 只查不改。

    三档：
        workspace  只能动它自己那个任务目录
        home       能动用户目录（桌面、文档、下载）
        full       全部
    """
    name = (name or "").strip()
    level = (level or "").strip().lower()

    try:
        cur = _api.call("/api/perms")
    except _api.ApiError as e:
        return f"查权限没走通：{e}"

    labels = cur.get("labels") or {}
    tasks = cur.get("tasks") or []

    def _label(lv: str) -> str:
        return str(labels.get(lv) or lv)

    # 只查
    if not level:
        if not name:
            if not tasks:
                return "现在还没有任何任务窗口。"

            lines = [f"共 {len(tasks)} 个窗口："]
            for t in tasks[:20]:
                if not isinstance(t, dict):
                    continue
                n = t.get("name") or t.get("id") or "?"
                lv = t.get("level") or "workspace"
                lines.append(f"  · {n}  —— {_label(lv)}")
            return "\n".join(lines)

        for t in tasks:
            if isinstance(t, dict) and (t.get("name") == name or t.get("id") == name):
                lv = t.get("level") or "workspace"
                return f"「{name}」现在的权限是：{_label(lv)}（{lv}）"

        return f"没有叫「{name}」的窗口。"

    # 改
    if not name:
        return "要改哪个窗口的权限？告诉我窗口名。"
    if level not in ("workspace", "home", "full"):
        return f"权限档只有 workspace / home / full 三种，不认识「{level}」。"

    # ⚠️ 有码就直接提交，**不要再调一次"升权"**。
    #
    #    实测（2026-11-04）撞到的坑：
    #
    #        第一次调 /api/perms（不带码）→ 引擎发码 A
    #        用户把 A 报过来 → 我又调 /api/perms（带 A）
    #        → 引擎**又发了一个新码 B**，把 A 作废了
    #        → 提交失败，而且用户手里的 A 变成废的
    #
    #    原因：引擎那层的判断是"没有码就发码"，
    #    它看不出这次调用是"想提交"还是"想发起"。
    #
    #    所以：**带 code 时就直接走校验**，不带才发码。
    payload = {"name": name, "level": level}
    if code:
        payload["code"] = code

    try:
        d = _api.call("/api/perms", payload)
    except _api.ApiError as e:
        return f"改权限没走通：{e}"

    # ⚠️ 升权要验证码 —— 引擎会发码，然后在返回里说 need_code。
    #
    #    以前这里只读 d["error"]，把引擎发的 message 吞了，
    #    用户看到的是"没改成：未知原因"，一脸懵。
    #
    #    实测（2026-11-04）：前端明明收到
    #        {"ok": False, "need_code": True,
    #         "message": "验证码已发到 sx***@163.com"}
    #    却显示成未知原因。

    if d.get("need_code"):
        frm = d.get("from") or ""
        to = d.get("to") or level
        msg = str(d.get("message") or "").strip()

        lines = [
            f"「{name}」从「{_label(frm)}」提到「{_label(to)}」，",
            "这一步要验证码 —— 放宽权限是这个系统最不能随便松的一步。",
        ]
        if msg:
            lines.append("")
            lines.append(msg)
        lines.append("")
        lines.append("用户把码报过来，我再提交一次。")

        return "\n".join(lines)

    if d.get("ok") is False:
        err = d.get("error") or d.get("message") or "未知原因"
        return f"没改成：{err}"

    return f"✅ 「{name}」的权限改成了：{_label(level)}（{level}）"


def guard_status() -> str:
    """验证码那块配好没有。

    用户问"验证码怎么收不到"的时候，先看这个。
    """
    try:
        d = _api.call("/api/guard")
    except _api.ApiError as e:
        return f"查不到：{e}"

    enabled = bool(d.get("enabled"))
    email = str(d.get("email_masked") or d.get("email") or "")
    smtp = str(d.get("smtp_host") or "")

    lines = []

    if not enabled:
        lines.append("验证码闸门没开。危险操作不会拦，也不会发码。")
        lines.append("要开的话去桌面端「设置」里配邮箱。")
        return "\n".join(lines)

    lines.append("验证码闸门开着。")

    if email and smtp:
        lines.append(f"码发到：{email}（{smtp}）")
        lines.append("用户没收到的话：看垃圾邮件，或者桌面端「设置」里点一下测试。")
    else:
        lines.append("⚠️ 邮箱没配全，码可能发不出去。")

    return "\n".join(lines)
