"""推送 —— 任务跑完了，攒一批主动报到。

## 这套逻辑哪来的

    是我们自己的。之前跑在 AstrBot 里（plugins/astrbot_plugin_dsh_push.py），
    AstrBot 只负责"往哪个会话发"，其余全是我们的代码：

        · outbox.jsonl      引擎写的（remote-dispatch 的）
        · delivered 标记    我们自己写的
        · 定时轮询          我们自己写的
        · 按模型分组汇总    我们自己写的

    现在搬到 atrium：
        引擎还是 remote-dispatch，outbox 还是它写
        发送改成 atrium 自己的平台接口
        **不再需要 AstrBot**

## 为什么要有队列和已读标记

    任务跑完就发一条，会刷屏：

        ✅ 跑完了：登录页
        ✅ 跑完了：接口核对
        ✅ 跑完了：数据分析
            ↓
        **三条消息，用户要划着看**

    而且中途断线会漏。

    所以要：**先攒，再一次报，报过的记下来。**

## ⚠️ 为什么 not 直接读 delivered.json

    那是 AstrBot 插件的文件格式，跟 atrium 不是一回事。

    我们用自己的：~/.atrium/pushed.json
    存 {"ids": [...]} —— 报过的任务名列表。
    只留最近 2000 个，防止无限长。

## 分档

    1-3 个    → 逐条说清楚，结果正文也给
    4-20 个   → 按模型分组列清单
    20+ 个    → 只报统计 + 失败的

    为什么分档：3 条以内用户想看细节，20 条以上看细节是折磨。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time


# ══════════════════════════════════════════════════════
# 已读标记
# ══════════════════════════════════════════════════════

class Pushed:
    """记下哪些任务已经报过了。

    ⚠️ 落盘而不是放内存 —— 进程重启后不能重报一遍。
       实测过：重启后会把过去所有任务再推一遍，用户以为出了大事。
    """

    def __init__(self, path: str = "", cap: int = 2000) -> None:
        home = os.path.expanduser("~")
        self.path = path or os.path.join(home, ".atrium", "pushed.json")
        self.cap = max(100, int(cap))
        self._lock = threading.Lock()
        self._ids: list[str] = []
        self._set: set[str] = set()
        self._load()

    def _load(self) -> None:
        try:
            if not os.path.exists(self.path):
                return
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            ids = d.get("ids") if isinstance(d, dict) else None
            if isinstance(ids, list):
                self._ids = [str(x) for x in ids][-self.cap:]
                self._set = set(self._ids)
        except (OSError, json.JSONDecodeError):
            pass

    def seen(self, task_id: str) -> bool:
        with self._lock:
            return str(task_id) in self._set

    def mark(self, task_ids: list[str]) -> None:
        """标记一批已报过。"""
        if not task_ids:
            return

        with self._lock:
            for t in task_ids:
                t = str(t)
                if t and t not in self._set:
                    self._ids.append(t)
                    self._set.add(t)

            # 只留最近 cap 个
            if len(self._ids) > self.cap:
                drop = self._ids[:-self.cap]
                self._ids = self._ids[-self.cap:]
                for t in drop:
                    self._set.discard(t)

            self._save_locked()

    def _save_locked(self) -> None:
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"ids": self._ids}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass


# ══════════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════════

def _profile_label(pid: str) -> str:
    """模型 id 转成人看的名字。

    remote-dispatch 的 profile 有两种形态：
        · "p1793460955495"   内部 id
        · "deepseek"         名字

    内部 id 要去 engine.json 里查真名。
    """
    pid = (pid or "").strip()
    if not pid:
        return ""

    # 已经是人看的名字就直接用
    if not pid.startswith("p") or not pid[1:].isdigit():
        return pid[0].upper() + pid[1:] if pid.isascii() else pid

    try:
        cfg = os.path.join(os.environ.get("APPDATA") or "", "dsh-bridge", "engine.json")
        with open(cfg, encoding="utf-8") as f:
            d = json.load(f)
        items = d.get("profiles") or []
        if isinstance(items, dict):
            items = list(items.values())
        for x in items:
            if isinstance(x, dict) and x.get("id") == pid:
                return str(x.get("name") or pid)
    except (OSError, json.JSONDecodeError):
        pass

    return pid


def _files_of(rec: dict) -> list[str]:
    """这个任务产出了哪些文件（给文字用的短名字）。

    ⚠️ 走的是同一个 collect_files，所以"表格里的产出列"
       和"真的发出去的文件"永远一致。

       以前这里是单独扫工作目录的，
       结果表格显示 —，但文件其实在桌面
       （任务写到桌面去了）。
    """
    paths = collect_files([rec])

    res = []
    for p in paths[:5]:
        name = os.path.basename(p)
        try:
            size = os.path.getsize(p)
        except OSError:
            res.append(name)
            continue

        if size < 1024:
            res.append(f"{name}({size}B)")
        elif size < 1024 * 1024:
            res.append(f"{name}({size // 1024}KB)")
        else:
            res.append(f"{name}({size // 1024 // 1024}MB)")
    return res


def _task_start_time(rec: dict) -> float:
    """任务开始的时间戳（秒）。

    ⚠️ 这是过滤"这次产出的文件"的基准。

        实测（2026-11-04）：
            工作目录里躺着三次任务写的 你好.md / 你好2.md / 你好3.md，
            我只该发这次那个（你好3.md），
            但直接扫目录会把三个全发出去 ——
            用户收到三条文件，还以为是三个产��。

        规则：文件修改时间 >= 任务开始时间 → 算这次的。
    """
    import datetime

    for key in ("started_at", "created_at"):
        s = str(rec.get(key) or "").strip()
        if not s:
            continue
        try:
            dt = datetime.datetime.fromisoformat(s)
            return dt.timestamp()
        except (ValueError, TypeError):
            continue

    # 拿不到时间就不限制（宁可多发，不能漏发）
    return 0.0


def collect_files(recs: list[dict]) -> list[str]:
    """把一批任务产出的**文件绝对路径**收集起来。

    ## 为什么不跟 build_digest 合并

        build_digest 拼的是"给人看的文字"（"派发测试.docx(34KB)"），
        发文件要的是"绝对路径"。

        两个需求不同，分开放更清楚。

    ## 挑哪些

        只挑**真正的产出**：

            ✅ .docx .xlsx .pptx .pdf .txt .md .csv .json
            ✅ .png .jpg .gif .zip .html

            ❌ .py .js .sh（那是过程脚本，不是结果）
            ❌ 日志、__pycache__、隐藏文件

        ⚠️ 实测（2026-11-04）：用户要的是"做的文档推回来"。
           脚本发过去反而添乱。
    """
    want = (
        ".docx", ".xlsx", ".pptx", ".pdf", ".txt", ".md", ".csv",
        ".json", ".png", ".jpg", ".jpeg", ".gif", ".zip", ".html",
    )

    out: list[str] = []
    seen: set[str] = set()

    def _add(p: str, since: float = 0.0) -> None:
        p = (p or "").strip().strip('"\'`')
        if not p or p in seen:
            return
        if not p.lower().endswith(want):
            return
        try:
            if not os.path.isfile(p):
                return
            # ⚠️ 只收"任务开始之后改过的"。
            #    不然会把上次任务留下的文件也发出去。
            if since and os.path.getmtime(p) < since - 1.0:
                return
        except OSError:
            return
        seen.add(p)
        out.append(p)

    for r in recs:
        since = _task_start_time(r)

        # ── 一、扫任务自己的工作目录 ──
        sid = str(r.get("session_id") or "").strip()
        if sid and os.path.isdir(sid):
            try:
                names = os.listdir(sid)
            except OSError:
                names = []

            for name in names:
                if name.startswith(".") or name == "__pycache__":
                    continue
                if not name.lower().endswith(want):
                    continue
                _add(os.path.join(sid, name), since)

        # ── 二、从 result 里抠路径 ──
        #
        # ⚠️ 任务可能把文件写到桌面、下载目录或者别的地方。
        #    实测（2026-11-04）：
        #        「写文档DS」把 docx 写到 C:\Users\19836\Desktop\
        #        工作目录里什么都没有，所以只扫目录会漏。
        #
        #    从 result 的文本里找 Windows 路径。
        body = str(r.get("result") or "") + "\n" + str(r.get("error") or "")
        if body.strip():
            # C:\xxx\yyy.docx 这种形状
            for m in re.finditer(r'[A-Za-z]:[\\/][^\s"\'<>|*?\r\n]{1,200}',
                                 body):
                _add(m.group(0), since)

    return out


def build_digest(recs: list[dict]) -> str:
    """把一批跑完的任务汇总成一条消息。

    ## 分档

        1-3 个    → 逐条说清楚，带结果正文
        4-20 个   → 按模型分组列清单
        20+ 个    → 只报统计和失败的

    ## 为什么按模型分组

        同时开 DeepSeek 和 Kimi 的时候，一批里有好几个模型的任务。
        不分组的话用户看不出哪个是哪个 ——
        而这恰恰最要紧：哪个快、哪个慢、哪个老出错。

        这条是踩出来的，不是想出来的。
    """
    n = len(recs)
    if n == 0:
        return ""

    ok = [r for r in recs if str(r.get("status") or "") == "ok"]
    bad = [r for r in recs if str(r.get("status") or "") != "ok"]

    # ── 1 个：逐条说清楚 ──
    #
    # ⚠️ 分档改成"1 个用列表，2 个以上用表格"。
    #
    #    原来是 1-3 个都用列表，结果 2 个任务也是列表 ——
    #    用户的原话：「两个任务现在也变成列表了，不是一个也变成列表了，
    #    不是表格。A：如果是两个，产出那一格应该放文档」。
    #
    #    对：只要不止一个，就该能横向对比，那就得用表格。
    if n <= 1:
        lines = []
        for r in recs:
            lines.append(_one_task(r, detail=True))
        return "\n\n".join(lines)

    # ── 20+ 个：只报统计 ──
    if n > 20:
        lines = [f"**{n} 个任务跑完了**：成功 {len(ok)}，失败 {len(bad)}"]

        if bad:
            lines.append("")
            lines.append("失败的：")
            for r in bad[:10]:
                who = _profile_label(r.get("profile") or "")
                name = _short(r)
                err = (r.get("error") or r.get("status") or "")[:60]
                lines.append(f"- [{who or '默认'}] {name}：{err}")
            if len(bad) > 10:
                lines.append(f"- ……还有 {len(bad) - 10} 个")
        return "\n".join(lines)

    # ── 2-20 个：按模型分组的表格 ──
    #
    # ⚠️ 用表格而不是 bullet 列表。
    #
    #    用户的原话：「弄成表格分类，deepseek、kimi、千问……
    #    然后是任务名称，干了啥，文件啊这些是可以一起推送回来的」
    #
    #    列表看不出对比（哪个模型快、哪个出错多），
    #    表格一眼就能扫。
    groups: dict[str, list[dict]] = {}
    for r in recs:
        who = _profile_label(r.get("profile") or "") or "默认"
        groups.setdefault(who, []).append(r)

    # 组按"完成的条数"排，多的在前
    order = sorted(groups.keys(), key=lambda k: -len(groups[k]))

    lines = [f"**{n} 个任务跑完了**：成功 {len(ok)}，失败 {len(bad)}"]
    lines.append("")

    for who in order:
        rows = groups[who]

        lines.append(f"**{who}**（{len(rows)} 个）")
        lines.append("")
        lines.append("| 任务 | 状态 | 耗时 | 产出 |")
        lines.append("|---|---|---|---|")

        for r in rows:
            is_ok = str(r.get("status") or "") == "ok"
            mark = "✅" if is_ok else "❌"

            name = str(r.get("name") or r.get("id") or "")
            if len(name) > 20:
                name = name[:20] + "…"

            secs = r.get("seconds")
            t = ""
            if secs is not None:
                try:
                    t = f"{float(secs):.0f}s"
                except (TypeError, ValueError):
                    pass

            files = _files_of(r)
            if files:
                # ⚠️ 表格里不能有 | 和换行，会把表拆坏
                fcell = "、".join(files).replace("|", "/").replace("\n", " ")
                if len(fcell) > 40:
                    fcell = fcell[:40] + "…"
            else:
                st = str(r.get("status") or "")
                fcell = "—" if is_ok else (r.get("error") or st or "—")[:30]
                fcell = str(fcell).replace("|", "/").replace("\n", " ")

            lines.append(f"| {name} | {mark} | {t} | {fcell} |")

        lines.append("")

    return "\n".join(lines).rstrip()


def _short(r: dict) -> str:
    """任务名，太长截断。"""
    name = str(r.get("name") or r.get("id") or "")
    return name[:24] + "…" if len(name) > 24 else name


def _one_task(r: dict, detail: bool = True) -> str:
    """单个任务的一段话。"""
    ok = str(r.get("status") or "") == "ok"
    head = "✅ 跑完了" if ok else "❌ 没跑成"

    name = str(r.get("name") or r.get("id") or "")
    who = _profile_label(r.get("profile") or "")
    secs = r.get("seconds")

    bits = []
    if who:
        bits.append(f"模型 {who}")
    if secs is not None:
        try:
            bits.append(f"{float(secs):.1f} 秒")
        except (TypeError, ValueError):
            pass

    lines = [f"{head}：**{name}**"]
    if bits:
        lines.append("（" + "，".join(bits) + "）")

    if detail:
        body = (r.get("result") if ok else (r.get("error") or r.get("result"))) or ""
        body = str(body).strip()
        if body:
            if len(body) > 500:
                body = body[:500] + "…"
            lines.append("")
            lines.append(body)

    files = _files_of(r)
    if files:
        lines.append("")
        lines.append("产出：" + " ".join(files))

    return "\n".join(lines)
