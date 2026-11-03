"""监工 —— 盯着派出去的任务，跑完一批就攒着一起报。

## 跟之前那版的区别

    之前（错的）：
        每 3 秒查一次，**发现一个就发一条**
            ↓
        · 10 个任务跑完了 → 10 条消息，刷屏
        · 没有已读标记 → 重启后会重报
        · 没有汇总 → 看不出哪个模型快

    现在（对的）：
        每 N 秒查一批
            ↓
        **攒起来合成一条**（按模型分组）
            ↓
        发出去，标记已读
            ↓
        重启不会重报，也不会一条一条刷屏

## 这两套逻辑是哪儿来的

    我们自己写的。之前跑在 AstrBot 插件里，
    AstrBot 只提供"往哪个会话发"的能力，
    其余（队列、标记、汇总、轮询）全是我们的代码。

    现在搬到 atrium：
        发送改成 atrium 自己的平台接口
        **不再需要 AstrBot**

## ⚠️ 什么时候算"一批"

    一次轮询里发现的所有新完成任务 = 一批。

    如果任务跑得慢、隔很久才完一个，那也会一次报一个 ——
    那是正常的（用户也想早点知道）。

    但如果 10 个任务在 2 秒内接连完成，
    它们会在同一个轮询周期里被一起发现，合成一条。
"""

from __future__ import annotations

import os
import threading
import time

from atrium.core.digest import Pushed, build_digest, collect_files

# 一个任务最多盯多久（超过就不盯了，避免内存里堆老任务）
_TTL = 30 * 60.0

# ⚠️ 等待窗口：任务跑完之后**再等这么久**才报。
#
#    为什么：
#        10 个任务里，有的 4 秒完、有的 10 秒完。
#        每 5 秒轮询一次，第一个跑完就被立刻报走了 ——
#        结果用户收到两条："4 个跑完了""4 个跑完了"，
#        剩下的还在跑，用户以为出问题了。
#
#    实测（2026-11-04）：就是这个现象。
#
#    加上这个窗口之后：
#        第一批跑完 → 不立刻报，记下时间 → 等 8 秒
#        这 8 秒里陆续跑完的，全算同一批
#        8 秒到了，一起报
_SETTLE = 8.0


class Watch:
    """盯任务，跑完一批攒起来主动报。"""

    def __init__(self, app, engine, interval: float = 5.0) -> None:
        self.app = app
        self.engine = engine

        # ⚠️ 5 秒是折中：
        #    太短（1 秒）会频繁问引擎，浪费
        #    太长（30 秒）用户等得不耐烦
        self.interval = max(2.0, float(interval))

        # 已读标记（落盘）
        self.pushed = Pushed()

        # task_id → 等它的会话列表
        self._who: dict[str, list[dict]] = {}
        self._when: dict[str, float] = {}

        # task_id → 第一次发现它跑完的时间（用来算等待窗口）
        self._finished: dict[str, float] = {}

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ══════════════════════════════════════════════════
    # 登记
    # ══════════════════════════════════════════════════

    def watch(self, task_id: str, msg) -> None:
        """记下：这个任务跑完了要报到这个会话。"""
        task_id = str(task_id or "").strip()
        if not task_id:
            return

        with self._lock:
            rows = self._who.setdefault(task_id, [])

            # 同一个会话不重复加
            for r in rows:
                if (r.get("platform") == msg.platform
                        and r.get("channel") == msg.channel):
                    return

            rows.append({
                "platform": msg.platform,
                "channel": msg.channel,
                "reply_to": msg.reply_to or msg.id,
                "meta": dict(msg.meta or {}),
            })
            self._when[task_id] = time.time()

    # ══════════════════════════════════════════════════
    # 线程
    # ══════════════════════════════════════════════════

    def start(self) -> None:
        if self._thread is not None:
            return

        def _loop() -> None:
            warned = False
            while not self._stop.is_set():
                try:
                    self._tick()
                    warned = False
                except Exception as e:  # noqa: BLE001
                    # ⚠️ 只警告一次，别刷屏。
                    #    这个循环要一直活到进程结束。
                    if not warned:
                        import traceback

                        traceback.print_exc()
                        print(f"[watch] 出错（会继续重试）："
                              f"{type(e).__name__}: {e}", flush=True)
                        warned = True

                self._stop.wait(self.interval)

        self._thread = threading.Thread(target=_loop, name="watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ══════════════════════════════════════════════════
    # 一轮
    # ══════════════════════════════════════════════════

    def _tick(self) -> None:
        now = time.time()

        with self._lock:
            # 清掉太老的
            for k in [k for k, t in self._when.items() if now - t > _TTL]:
                self._who.pop(k, None)
                self._when.pop(k, None)
                self._finished.pop(k, None)

            candidates = [
                k for k in self._who
                if not self.pushed.seen(k)
            ]

        if not candidates:
            return

        # 查状态
        done: list[dict] = []
        for task_id in candidates:
            try:
                st = self.engine.poll(task_id)
            except Exception:  # noqa: BLE001
                continue

            if not isinstance(st, dict):
                continue

            status = str(st.get("status") or "")

            # 还在跑
            if status in ("pending", "running", "", "queued", "None"):
                continue

            # 跑完了 —— 补上名字，汇总要用
            rec = dict(st)
            rec["id"] = task_id
            if not rec.get("name"):
                rec["name"] = task_id

            # ⚠️ 记下第一次发现它跑完的时间。
            #    然后判断等够没。
            with self._lock:
                fin = self._finished.get(task_id)
                if fin is None:
                    fin = now
                    self._finished[task_id] = fin

            # 还在等待窗口里 → 先不报，
            # 这几秒里陆续跑完的会并进同一批
            if now - fin < _SETTLE:
                continue

            done.append(rec)

        if not done:
            return

        # ⚠️ 第二批可能正好在报的时候也跑完了。
        #    这里再扫一次，把"已经等够窗口"的也拉进来，
        #    免得用户收到"4 个"紧接着又收"4 个"。
        with self._lock:
            extra_ids = [
                k for k in self._who
                if k not in {str(r.get("id") or "") for r in done}
                and not self.pushed.seen(k)
            ]

        for task_id in extra_ids:
            fin = self._finished.get(task_id)
            if fin is None or now - fin < _SETTLE:
                continue
            try:
                st = self.engine.poll(task_id)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(st, dict):
                continue
            status = str(st.get("status") or "")
            if status in ("pending", "running", "", "queued", "None"):
                continue
            rec = dict(st)
            rec["id"] = task_id
            if not rec.get("name"):
                rec["name"] = task_id
            done.append(rec)

        # 按会话分组：不同会话各报各的
        by_channel: dict[tuple[str, str], list[dict]] = {}
        for rec in done:
            task_id = str(rec.get("id") or "")
            with self._lock:
                targets = list(self._who.get(task_id) or [])

            for t in targets:
                key = (t.get("platform") or "", t.get("channel") or "")
                by_channel.setdefault(key, []).append(rec)

        # 逐会话汇总 + 发
        for (platform, channel), recs in by_channel.items():
            text = build_digest(recs)
            if not text:
                continue

            # 找回这个会话的 target（meta 要用）
            target = None
            for rec in recs:
                tid = str(rec.get("id") or "")
                with self._lock:
                    for t in (self._who.get(tid) or []):
                        if (t.get("platform"), t.get("channel")) == (platform, channel):
                            target = t
                            break
                if target:
                    break

            if target is None:
                continue

            self._deliver(target, text, recs)

        # 标记已读 + 清掉登记
        ids = [str(r.get("id") or "") for r in done]
        self.pushed.mark(ids)

        with self._lock:
            for tid in ids:
                self._who.pop(tid, None)
                self._when.pop(tid, None)
                self._finished.pop(tid, None)

        print(f"[watch] 报了一批 {len(done)} 个任务", flush=True)

    # ══════════════════════════════════════════════════
    # 送
    # ══════════════════════════════════════════════════

    def _deliver(self, target: dict, text: str, recs=None) -> None:
        """把汇总送到那个会话。"""
        from atrium.core.message import Reply

        # ⚠️ 把产出文件真的发过去。
        #
        #    以前只在文字里列文件名（"派发测试.docx(34KB)"），
        #    用户看到名字但拿不到文件 ——
        #    那不叫交付，那叫"告诉你有这么个东西"。
        #
        #    实测（2026-11-04）：用户明确要"做的文档推回来"。
        files: list[str] = []
        try:
            files = collect_files(recs or [])
        except Exception as e:  # noqa: BLE001
            print(f"[watch] 收集文件失败：{e}", flush=True)

        if files:
            print(f"[watch] 带 {len(files)} 个文件："
                  f"{[os.path.basename(f) for f in files]}", flush=True)

        r = Reply(
            platform=target.get("platform") or "",
            channel=target.get("channel") or "",
            reply_to=target.get("reply_to") or "",
            text=text,
            attachments=files,
            meta=dict(target.get("meta") or {}),
        )
        self.app.bus.push_reply(r)
        print(f"[watch] 推送 {target.get('platform')}：{text[:50]!r}", flush=True)
