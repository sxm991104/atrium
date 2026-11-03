"""atrium 主程序 —— 把平台、队列、引擎串起来。

## 数据流

    平台收到消息
        ↓
    push 进 inbox（落盘）
        ↓
    **立刻回"我收到了"**（给平台看）    ← 平台 3 秒超时才不会判死
        ↓
    后台线程：取 inbox → 判断 → 引擎 submit
        ↓
    引擎跑完 → 结果 push 进 outbox
        ↓
    发送线程：取 outbox → 平台 send

## ⚠️ 为什么判断放在后台而不是收消息时

    判断要调模型（"这是闲聊还是派活"），要几秒。
    放在收消息的同步路径上，平台就超时了。

## ⚠️ 为什么要有"立刻回"

    飞书 3 秒不回就重发，5 次之后判失败。
    扣子也类似。

    所以我们收到就回一句"收到"，
    真正的结果等跑完再推。

    这不是骗用户 —— 用户看到的是"已派出"，
    而不是"处理完了"。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

from atrium.core.message import Message, Reply
from atrium.core.queue import Bus
from atrium.engine.client import Engine, EngineError, make_engine
from atrium.platforms.base import Platform, PlatformError


# ══════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════

def config_path() -> str:
    """配置文件在哪。"""
    home = os.path.expanduser("~")
    return os.path.join(home, ".atrium", "config.json")


def load_config(path: str = "") -> dict:
    """读配置。没有就返回默认。"""
    p = path or config_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict, path: str = "") -> None:
    p = path or config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


# ══════════════════════════════════════════════════════
# 主程序
# ══════════════════════════════════════════════════════

class Atrium:
    """把平台、队列、引擎连起来。"""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.bus = Bus((cfg.get("storage") or {}).get("dir") or "")

        # 引擎
        ecfg = cfg.get("engine") or {}
        self.engine: Engine | None = None
        try:
            self.engine = make_engine(ecfg)
        except EngineError as e:
            print(f"[atrium] 引擎没起来：{e}", file=sys.stderr)

        # 平台
        self.platforms: dict[str, Platform] = {}
        for name, pcfg in (cfg.get("platforms") or {}).items():
            if not pcfg.get("enabled"):
                continue
            p = self._make_platform(name, pcfg)
            if p is not None:
                self.platforms[name] = p

        # 前台 Agent（在 atrium 里，不在 AstrBot 里）
        self._front = None
        self._llm = None
        self._build_front()

        # 进度监工：派出去的活跑完了，主动报到
        self.watch = None
        if self.engine is not None:
            try:
                from atrium.core.watch import Watch  # noqa: PLC0415

                self.watch = Watch(self, self.engine)
            except Exception as e:  # noqa: BLE001
                print(f"[atrium] 监工起不来：{e}", file=sys.stderr)

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

        # 每个平台最近一次"回哪"
        # 主动推送时，如果不知道回到哪，就用最近一次
        self._last_channel: dict[str, str] = {}

    def _build_front(self) -> None:
        """造前台 Agent。

        ⚠️ 模型配置从哪来：
           1. 配置文件里的 `front` 段
           2. 没有的话，借引擎那套（remote-dispatch 的 profiles）

        拿不到模型就退回"没前台"模式 ——
        那时候所有消息都直接派活（就是以前那个笨行为），
        但至少不会崩。
        """
        fcfg = self.cfg.get("front") or {}

        base = (fcfg.get("base_url") or "").strip()
        key = (fcfg.get("api_key") or "").strip()
        model = (fcfg.get("model") or "").strip()

        # 配置里没有就借引擎的
        if not (base and key):
            got = self._borrow_llm_config()
            if got:
                base, key, model = got

        if not (base and key):
            print("[atrium] 没有前台模型配置 —— 所有消息都会直接派活",
                  file=sys.stderr)
            return

        try:
            sys.path.insert(0, (self.cfg.get("engine") or {}).get("path") or "")
            from engine.llm import LLM  # noqa: PLC0415

            self._llm = LLM(
                base_url=base,
                api_key=key,
                model=model or "deepseek-flash",
            )
        except Exception as e:  # noqa: BLE001
            print(f"[atrium] 前台模型造不出来：{type(e).__name__}: {e}",
                  file=sys.stderr)
            return

        try:
            from atrium.agent.front import FrontAgent  # noqa: PLC0415

            self._front = FrontAgent(self._llm, self.engine)
            print(f"[atrium] 前台就绪（{model or '默认模型'}）", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[atrium] 前台造不出来：{type(e).__name__}: {e}",
                  file=sys.stderr)

    def _borrow_llm_config(self):
        """从 remote-dispatch 的 profiles 里借一套模型配置。"""
        try:
            import json as _json  # noqa: PLC0415
            import os as _os  # noqa: PLC0415

            cfg_dir = _os.environ.get("APPDATA") or _os.path.expanduser("~")
            # ⚠️ 文件名是 engine.json，不是 profiles.json。
            #    实测（2026-11-04）：按 profiles.json 找，永远找不到，
            #    结果前台说"没有模型配置"，所有消息都直接派活。
            p = _os.path.join(cfg_dir, "dsh-bridge", "engine.json")
            if not _os.path.exists(p):
                return None

            with open(p, encoding="utf-8") as f:
                d = _json.load(f)

            profiles = d.get("profiles") or {}
            if isinstance(profiles, list):
                profiles = {x.get("id"): x for x in profiles if isinstance(x, dict)}

            active = d.get("active") or ""
            one = profiles.get(active) if isinstance(profiles, dict) else None
            if not one:
                for v in (profiles.values() if isinstance(profiles, dict) else []):
                    if isinstance(v, dict) and v.get("api_key"):
                        one = v
                        break

            if isinstance(one, dict) and one.get("api_key"):
                return (
                    one.get("base_url") or "",
                    one.get("api_key") or "",
                    one.get("model") or "",
                )
        except Exception:  # noqa: BLE001
            pass

        return None


    def _make_platform(self, name: str, pcfg: dict) -> Platform | None:
        """按名字造一个平台。

        ⚠️ 这里不认识任何具体平台。
           平台自己往 registry 里登记（见 platforms/registry.py）。

           加一个平台 = 加一个文件 + 在 platforms/__init__.py 里导入一行。
           不用改这里。
        """
        from atrium.platforms import registry  # noqa: PLC0415

        p = registry.make_platform(name, pcfg)
        if p is None:
            print(
                f"[atrium] 没有这个平台：{name}"
                f"（已支持：{', '.join(registry.names())}）",
                file=sys.stderr,
            )
        return p


    def start(self) -> None:
        """起所有线程。"""
        print(f"[atrium] 启动：{len(self.platforms)} 个平台", flush=True)

        for name, p in self.platforms.items():
            t = threading.Thread(
                target=self._run_platform,
                args=(p,),
                name=f"plat-{name}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

        # 处理 inbox 的线程
        t2 = threading.Thread(target=self._worker_loop, name="worker", daemon=True)
        t2.start()
        self._threads.append(t2)

        # 发 outbox 的线程
        t3 = threading.Thread(target=self._sender_loop, name="sender", daemon=True)
        t3.start()
        self._threads.append(t3)

        # 进度监工
        if self.watch is not None:
            self.watch.start()
            print("[atrium] 进度监工已启动", flush=True)

    def _run_platform(self, p: Platform) -> None:
        """跑一个平台的接收循环。"""
        def on_msg(msg: Message) -> None:
            self._on_incoming(msg)

        while not self._stop.is_set():
            try:
                p.start(on_msg)
            except PlatformError as e:
                print(f"[atrium] {p.name} 出错：{e}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[atrium] {p.name} 意外：{type(e).__name__}: {e}", flush=True)

            if self._stop.is_set():
                break
            time.sleep(5)

    def stop(self) -> None:
        self._stop.set()
        if self.watch is not None:
            try:
                self.watch.stop()
            except Exception:  # noqa: BLE001
                pass
        for p in self.platforms.values():
            try:
                p.stop()
            except Exception:  # noqa: BLE001
                pass

    # ══════════════════════════════════════════════════
    # 收消息
    # ══════════════════════════════════════════════════

    def _on_incoming(self, msg: Message) -> None:
        """收到一条消息。

        ⚠️ 这个函数必须**快**。
           飞书 3 秒不回就重发。

           所以这里只做两件事：
               一、记下"最近回到哪"
               二、写进 inbox
           剩下的交给后台线程。
        """
        try:
            self._last_channel[msg.platform] = msg.channel
        except Exception:  # noqa: BLE001
            pass

        try:
            self.bus.push_message(msg)
            print(f"[atrium] 收到 {msg.short()}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[atrium] 写 inbox 失败：{e}", file=sys.stderr)

    # ══════════════════════════════════════════════════
    # 处理（后台）
    # ══════════════════════════════════════════════════

    def _worker_loop(self) -> None:
        """从 inbox 取消息，交给引擎。"""
        while not self._stop.is_set():
            try:
                msgs = self.bus.pop_messages()
            except Exception as e:  # noqa: BLE001
                print(f"[atrium] 读 inbox 失败：{e}", file=sys.stderr)
                msgs = []

            for m in msgs:
                try:
                    self._handle(m)
                except Exception as e:  # noqa: BLE001
                    print(f"[atrium] 处理失败：{type(e).__name__}: {e}",
                          file=sys.stderr)
                    try:
                        self.bus.mark_failed(m.to_dict(), str(e))
                    except Exception:  # noqa: BLE001
                        pass

            time.sleep(0.5)

    def _handle(self, msg: Message) -> None:
        """处理一条消息。

        ⚠️ 走前台 Agent（如果配了模型）。
           没配就退回"直接派活"—— 笨，但不会崩。

        为什么单独起线程：
            前台要调模型，一次几秒。
            串行处理的话，用户连发三条就要等三轮模型。
        """
        import threading as _th

        t = _th.Thread(target=self._handle_one, args=(msg,), daemon=True)
        t.start()

    def _handle_one(self, msg: Message) -> None:
        """真正处理一条消息。"""
        text = (msg.text or "").strip()

        if not text:
            self.bus.mark_done(msg.to_dict(), "空消息")
            return

        if self.engine is None:
            r = Reply.to(msg, "还没有配置引擎。打开 config.json 填一下。")
            self.bus.push_reply(r)
            self.bus.mark_done(msg.to_dict(), "无引擎")
            return

        # ── 有前台：让它想 ──
        if self._front is not None:
            # ⚠️ 带上这个会话的历史。
            #
            #    不带的话，用户说「看看进度」，
            #    前台根本不知道"刚才派了什么"，
            #    只能去 task_list 列全部 20 个任务 ——
            #    用户看到一堆老任务，懵。
            #
            #    实测（2026-11-04）：就是这个情况。
            try:
                from atrium.agent import memory as _mem

                hist = _mem.shared().history(msg.platform, msg.channel)
            except Exception:  # noqa: BLE001
                hist = []

            # ⚠️ 清空上一轮的"派了什么"。
            #    前台每轮可能派 0 个或多个任务，要按轮算。
            try:
                from atrium.agent import tools as _ftools

                _ftools.take_dispatched()
            except Exception:  # noqa: BLE001
                pass

            try:
                answer = self._front.reply(text, history=hist)
            except Exception as e:  # noqa: BLE001
                import traceback

                traceback.print_exc()
                print(f"[atrium] 前台出错：{type(e).__name__}: {e}",
                      file=sys.stderr)
                answer = f"我这边出错了：{type(e).__name__}"

            if answer:
                # 记进历史，下一轮能接上
                try:
                    from atrium.agent import memory as _mem

                    _mem.shared().add_user(msg.platform, msg.channel, text)
                    _mem.shared().add_assistant(
                        msg.platform, msg.channel, answer
                    )
                except Exception:  # noqa: BLE001
                    pass

                # ⚠️ 调试：把原话存下来，看清换行到底在不在。
                #
                #    用户在钉钉里看到表格挤成一行，
                #    但看不出是"模型没给换行"还是"发送时吃了换行"。
                try:
                    import time as _t

                    with open(r"D:\Project\atrium\_reply_dump.log", "a",
                              encoding="utf-8") as _f:
                        _f.write(
                            f"\n===== {_t.strftime('%H:%M:%S')} =====\n"
                            f"{answer!r}\n"
                        )
                except OSError:
                    pass

                # ⚠️ 把这一轮派出去的任务登记给监工。
                #    跑完了它主动报到。
                try:
                    from atrium.agent import tools as _ftools

                    dispatched = _ftools.take_dispatched()
                    if dispatched and self.watch is not None:
                        for tid in dispatched:
                            self.watch.watch(tid, msg)
                        print(f"[atrium] 盯上了 {len(dispatched)} 个任务："
                              f"{dispatched}", flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"[atrium] 登记监工失败：{e}", file=sys.stderr)

                self.bus.push_reply(Reply.to(msg, answer))
                self.bus.mark_done(msg.to_dict(), "前台答复")
            else:
                self.bus.mark_done(msg.to_dict(), "前台空答复")
            return

        # ── 没前台：直接派活（旧行为）──
        try:
            res = self.engine.submit(text)
        except EngineError as e:
            r = Reply.to(msg, f"派活失败：{e}")
            self.bus.push_reply(r)
            self.bus.mark_failed(msg.to_dict(), str(e))
            return

        if not res.get("ok"):
            r = Reply.to(msg, f"派活失败：{res.get('error') or '未知错误'}")
            self.bus.push_reply(r)
            self.bus.mark_failed(msg.to_dict(), str(res.get("error")))
            return

        task_id = res.get("id") or ""
        r = Reply.to(
            msg,
            f"已经派下去了，任务叫「{task_id}」。\n"
            f"跑完我会告诉你。想知道进度就问我。",
        )
        r.meta["task_id"] = task_id
        self.bus.push_reply(r)
        self.bus.mark_done(msg.to_dict(), f"派出 {task_id}")


    def _sender_loop(self) -> None:
        """从 outbox 取回复，发给平台。"""
        while not self._stop.is_set():
            try:
                replies = self.bus.pop_replies()
            except Exception as e:  # noqa: BLE001
                print(f"[atrium] 读 outbox 失败：{e}", file=sys.stderr)
                replies = []

            for r in replies:
                # ⚠️ 调试：看 attachments 到底有没有穿过 outbox。
                print(f"[_SENDER_DBG] 取到 reply attachments={r.attachments} "
                      f"meta_keys={list((r.meta or {}).keys())} "
                      f"hook={(r.meta or {}).get('session_webhook', '')[:50]!r}",
                      flush=True)

                p = self.platforms.get(r.platform)
                if p is None:
                    print(f"[atrium] 没有平台 {r.platform}，丢弃", file=sys.stderr)
                    continue

                ok = False
                for attempt in range(3):
                    try:
                        ok = p.send(r)
                    except Exception as e:  # noqa: BLE001
                        print(f"[atrium] 发失败：{type(e).__name__}: {e}",
                              file=sys.stderr)
                        ok = False

                    if ok:
                        break
                    time.sleep(1 + attempt)

                if ok:
                    print(f"[atrium] 发出 {r.text[:30]!r}", flush=True)

                    # ⚠️ 附件单独发。
                    #    文字部分是"告诉你有文件"，
                    #    这一步才是"把文件给你"。
                    if r.attachments:
                        print(f"[SEND_FILE_DEBUG] 有附件，准备发 "
                              f"platform={r.platform} "
                              f"has_send_file={hasattr(p, 'send_file')}",
                              flush=True)
                        sendf = getattr(p, "send_file", None)
                        if callable(sendf):
                            try:
                                res = sendf(r)
                                print(f"[SEND_FILE_DEBUG] send_file 返回 {res}",
                                      flush=True)
                            except Exception as e:  # noqa: BLE001
                                print(f"[atrium] 发附件失败："
                                      f"{type(e).__name__}: {e}",
                                      file=sys.stderr)
                        else:
                            print(f"[atrium] {r.platform} 不支持发文件",
                                  file=sys.stderr)
                else:
                    print(f"[atrium] 三次都发不出去：{r.text[:30]!r}",
                          file=sys.stderr)

            time.sleep(0.5)


# ══════════════════════════════════════════════════════
# 进程入口
# ══════════════════════════════════════════════════════

def run(cfg_path: str = "") -> int:
    cfg = load_config(cfg_path)
    if not cfg:
        print("[atrium] 没有配置。先跑 `python -m atrium.cli init`", file=sys.stderr)
        return 1

    app = Atrium(cfg)
    app.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[atrium] 收到中断，退出", flush=True)
        app.stop()
        time.sleep(1)

    return 0
