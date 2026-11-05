"""前台切安全模式 —— **必须验证码，而且只能升不能降。**

## 老爸定的规矩（2026-11-05）

    「要那个码，到前台了都要码宝贝，因为前台是暴露在外的，
      桌面端是用户电脑。」

    「前台只能去开，不能关。」

## 为什么前台必须比桌面端更严

    桌面端     用户自己坐在电脑前，看一眼屏幕就知道自己在干什么。
    前台       消息从飞书/钉钉进来，可能是别人借手机发的、
               可能是群里的陌生人、也可能是一条伪装过的文本。

    所以：
        **凡是"降低保护"的动作，前台一律不给做。**

## "升"和"降"怎么算

    保护强度（guard/capabilities.py 里的 STRENGTH）：

        strict（严格防护）  3    什么都拦，但要码
        both（全开）        2    不拦，但有回退
        rollback（出事可回退）1   不拦，只有回退

    ⚠️ 这里有个反直觉的地方：

        "严格防护"看上去最安全，但它**每动一下都要码**。
        用户嫌烦就会去把它调松 —— 而"调松"恰恰是危险的。

        所以 STRENGTH 的排序不按"名字听起来多安全"，
        按"实际兜底有多厚"：
            严格防护  三层（守卫 + 码 + 回退）
            全开      两层（码 + 回退）→ 其实这里也没码了，只有回退
            出事可回退 一层（只有回退）

        当前值不能往下走，只能往上走或者不动。

## 需要码的判断

    ⚠️ 不管切到哪个，**一律要码**。不是"升就不要降就要"。

        因为前台没有身份概念 —— 你没法确认说话的是不是本人。
        所以每一次改动都要本人手里的那个码。
"""

from __future__ import annotations

from atrium.engine import api as _api


# 保护强度。数字越大，出事时兜底越厚。
#
# ⚠️ 这个表必须跟 guard/capabilities.py 里的 STRENGTH 一致。
#    两边不一致的话，前台放过去的请求会被服务端拒掉（或者反过来），
#    用户看到的就是"点了没反应"。
STRENGTH = {
    "strict": 3,
    "both": 2,
    "rollback": 1,
}

LABEL = {
    "strict": "严格防护",
    "both": "全开",
    "rollback": "出事可回退",
}


def set_security_mode(mode: str = "", code: str = "") -> str:
    """切安全模式。**必须带验证码。**

    mode 留空 = 只查现在是什么模式。

    参数：
        mode   strict / both / rollback
        code   验证码（不填会先发一个到你邮箱）

    ## 为什么先查再改

        前台发过来的请求可能在飞书上排队很久才被处理 ——
        等它到的时候，用户可能已经在桌面端改过了。

        所以先读当前状态，再判断：
            · 跟当前一样        → 什么都不做，告诉它已经是了
            · 比当前弱          → **拒绝**，让用户自己去桌面端
            · 比当前强或平      → 要码，然后切
    """
    want = (mode or "").strip().lower()

    # ── 先验模式名（本地就能验，不用连引擎）──
    #
    # ⚠️ 顺序很重要：**先验名字，再连引擎**。
    #
    #    原来先查当前状态，引擎没起就直接返回
    #        「查当前安全模式没走通：连不上引擎」
    #    于是"模式名写错"这个错根本看不到 ——
    #    用户以为是自己拼错了，其实是服务没起；
    #    或者反过来，服务没起他却在改自己的拼写。
    #
    #    名字对不对是**本地就能判**的，不该依赖网络。
    #    实测（2026-11-05）：测试撞出来的。
    if want and want not in STRENGTH:
        return (
            f"没有「{want}」这个模式。"
            "只有：严格防护（strict）/ 全开（both）/ 出事可回退（rollback）。"
        )

    # ── 再读当前状态 ──
    try:
        cur = _api.call("/api/security/mode")
    except _api.ApiError as e:
        return f"查当前安全模式没走通：{e}"

    cur_mode = str(cur.get("mode") or "").strip().lower()
    cur_label = str(cur.get("label") or LABEL.get(cur_mode) or cur_mode)

    # 只查
    if not want:
        on = []
        f = cur.get("features") or {}
        if f.get("guard"):
            on.append("动作守卫")
        if f.get("shadow"):
            on.append("影子执行")
        if f.get("audit"):
            on.append("审核员")
        if f.get("code"):
            on.append("验证码")
        if f.get("rewind"):
            on.append("版本回退")

        lines = [f"现在是「{cur_label}」：{' + '.join(on) if on else '什么都不开'}"]

        # 顺便说清哪些能切（省得用户白问一轮）
        try:
            caps = cur.get("capabilities") or {}
            modes = caps.get("modes") or {}
            if modes:
                can = [LABEL.get(k, k) for k, v in modes.items() if v.get("ok")]
                cannot = [
                    f"{LABEL.get(k, k)}（{v.get('why')}）"
                    for k, v in modes.items() if not v.get("ok")
                ]
                if can:
                    lines.append("能切的：" + "、".join(can))
                if cannot:
                    lines.append("开不了的：" + "；".join(cannot))
        except Exception:  # noqa: BLE001
            pass

        return "\n".join(lines)

    # ── 已经就是这个模式 ──
    if want == cur_mode:
        return f"现在就是「{LABEL[want]}」，不用改。"

    # ── 只能升不能降 ──
    #
    # ⚠️ 这一条是硬拦。
    #    不要因为"用户明确要求了"就放行 ——
    #    前台分不清"用户本人"和"某个能发消息的人"。
    if STRENGTH.get(want, 0) < STRENGTH.get(cur_mode, 0):
        return (
            f"不能这么改。\n\n"
            f"现在是「{cur_label}」，切到「{LABEL[want]}」等于**降低保护** —— "
            f"这种事只能你在电脑上自己点，我这边不给做。\n\n"
            f"（前台的消息可能是别人发的，所以凡是「变松」的操作一律拦住。）\n\n"
            f"要切的话，去桌面端「安全」页点一下。"
        )

    # ── 要码 ──
    #
    # ⚠️ 走 /api/security/mode/verified 这个入口，不是 /api/security/mode。
    #
    #    /api/security/mode          桌面端用的，不要码
    #                                （用户坐在电脑前，自己看着屏幕点）
    #    /api/security/mode/verified **前台用的，一刀切要码**
    #                                （消息可能是别人发的）
    #
    #    最早这里调的是不要码那个 —— 测试时发现
    #    "切好了，现在是严格防护"，一个码都没发。
    #    （2026-11-05 老爸的原话：
    #       「要那个码，到前台了都要码宝贝，因为前台是暴露在外的，
    #         桌面端是用户电脑。」）
    payload: dict = {"mode": want}
    if code:
        payload["code"] = code

    try:
        d = _api.call("/api/security/mode/verified", payload)
    except _api.ApiError as e:
        return f"切模式没走通：{e}"

    if not isinstance(d, dict):
        return "切模式没拿到结果。"

    if d.get("ok"):
        return (
            f"切好了，现在是「{LABEL.get(str(d.get('mode') or want), want)}」。"
        )

    # 需要码
    if d.get("need_code"):
        return (
            f"要切到「{LABEL[want]}」，得先验证一下。\n\n"
            f"{d.get('message') or '验证码发到配置的邮箱了，拿到之后告诉我。'}\n\n"
            f"把码报过来，我再帮你切。"
        )

    return f"没切成功：{d.get('error') or '没给原因'}"
