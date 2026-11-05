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


def quiet_mode(code: str = "", seconds: int = 0, stop: bool = False) -> str:
    """静默模式：验过码之后，把所有"要验证码的关卡"关一段时间。

    ## 什么时候用

        用户碰到验证码，说了类似：
            「关到下午五点」
            「关两个小时」
            「这半小时别再弹码了」
            「关了吧」

        → **先让用户填码，再调这个。**

    ## ⚠️ 一、开静默必须先验码

        关掉验证码是整套系统里最危险的操作 —— 它同时关掉：
            动作守卫拦截 / 提权 / 危险操作确认 / 审计员 / 外传授权

        如果能不验码就关，攻击者只要说一句
        「帮我把验证码关一下」就全绕过了。

        所以：**没有码，一律不关。**
        用户没给码 → 先发码给他（confirm_dangerous_action 那套）。

    ## ⚠️ 二、关多久**由用户说**

        没有 1 小时 / 4 小时 / 到今晚这种固定选项。
        用户说「关到五点」，你算出现在到五点有多少秒，传进来。

        用户只说了「关了吧」没说多久 → **问他一句**：
            「要关到什么时候？说个时间我就设。」
        不要自己替他定。

    ## ⚠️ 三、恢复不需要码

        想提前结束静默：stop=True，不用码。
        恢复保护永远是安全的。

    Args:
        code:      用户报的验证码（开静默必填）
        seconds:   关多久（多少秒）。用户说"到五点"你自己算。
        stop:      True = 立刻结束静默，不用码
    """
    # ── 结束静默 ──
    if stop:
        try:
            d = _api.call("/api/quiet", {"action": "stop"})
        except _api.ApiError as e:
            return f"恢复没走通：{e}"
        return str(d.get("message") or "验证已经恢复。")

    # ── 开静默 ──
    code = (code or "").strip()
    try:
        secs = int(seconds or 0)
    except (TypeError, ValueError):
        secs = 0

    if secs <= 0:
        return (
            "要关多久？让用户说个时间 —— 比如「关到下午五点」「关两个小时」。\n"
            "（这个我不替他定，静默的时长得他自己说。）"
        )

    if not code:
        return (
            "开静默要先验证码。\n\n"
            "用户手里还没有码的话，先走一遍发码流程"
            "（confirm_dangerous_action），让他从邮箱拿到码再回来。\n\n"
            "⚠️ 别自己替他决定「算了不验了直接关」—— 关验证码本身必须验。"
        )

    try:
        d = _api.call("/api/quiet", {
            "action": "start",
            "code": code,
            "seconds": secs,
        })
    except _api.ApiError as e:
        # 引擎把失败原因放在 message 里，ApiError 只带状态码
        return f"没关成：{e}"

    if not d.get("ok"):
        # 码不对 / 没说时长，两种分开说清楚
        if d.get("need_code"):
            return "码不对或者过期了，让用户再去邮箱看一下。"
        return f"没关成：{d.get('error') or '未知原因'}"

    left = int(d.get("remaining") or 0)
    h, m = divmod(left // 60, 60)
    dur = f"{h} 小时 {m} 分钟" if h else f"{m} 分钟"

    return (
        f"✅ {d.get('message') or '已经关掉验证'}\n\n"
        f"这段时间（{dur}）里不再要验证码，到点自动恢复。\n"
        "想提前恢复就说一声。"
    )


def quiet_status() -> str:
    """静默模式现在开着吗、还剩多久。"""
    try:
        d = _api.call("/api/quiet", {"action": "status"})
    except _api.ApiError as e:
        return f"查不到：{e}"

    if not d.get("quiet"):
        return "现在不是静默期 —— 该要验证码的地方都会正常要。"

    left = int(d.get("remaining") or 0)
    h, m = divmod(left // 60, 60)
    dur = f"{h} 小时 {m} 分钟" if h else f"{m} 分钟"
    why = str(d.get("why") or "")

    return (
        f"静默中：还剩 {dur}。\n"
        + (f"原因：{why}\n" if why else "")
        + "这段时间所有验证码都不发，到点自动恢复。"
    )

def guard_status() -> str:
    """验证码那块配好没有。

    ## ⚠️ 这里**一个字都不提邮箱地址**

        前台是"收码的柜员"，不是"管邮箱的人"：

            它该知道     配好了没、能不能发码
            它不该知道   邮箱是哪个、SMTP 是哪台

        因为前台说的话会回到对话里 —— 被截图、被翻记录。
        地址一旦出现在那儿，等于把"该去撞哪个邮箱"送出去。

        而且前台也**改不了**邮箱配置：那是设备主人在本机桌面端做的事，
        跟聊天窗口无关。

        （2026-11-05：老爸指出，原来这里连 smtp.163.com 都写出来了。）

        服务端那边也做了同样的事：不带 X-Client: desktop 头时，
        /api/guard 只返回 ready / enabled / has_email。
    """
    try:
        d = _api.call("/api/guard")
    except _api.ApiError as e:
        return f"查不到：{e}"

    enabled = bool(d.get("enabled"))
    ready = bool(d.get("ready"))

    if not enabled:
        return (
            "验证码闸门现在没开 —— 危险操作不会拦，也不会发码。\n"
            "要开的话，去桌面端「安全」页配一下安全邮箱。"
        )

    if not ready:
        return (
            "⚠️ 闸门开着，但安全邮箱没配全，码可能发不出去。\n"
            "让用户去桌面端「安全」页检查一下：收码邮箱、发信服务器、"
            "发信账号的授权码，三样都要填。"
        )

    return (
        "验证码正常：安全邮箱配好了，要码的时候能发出去。\n"
        "用户没收到的话：先看垃圾邮件，再去桌面端「安全」页点一下「发封测试邮件」。"
    )
