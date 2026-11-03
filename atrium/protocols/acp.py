"""atrium 的 ACP 服务端 —— 让 coze-bridge / Zed / 任何 ACP 客户端能起我们。

## ACP 是什么

    Agent Client Protocol，开放标准。
    JSON-RPC 2.0，一行一帧（ldjson），走 stdin/stdout。

    规范：https://agentclientprotocol.com/protocol/v1/overview

## 为什么 atrium 要做这个

    扣子的 coze-bridge 是个"翻译官"：

        Frontier（字节私有，WebSocket）
              ↕  coze-bridge 翻译
        ACP（开放标准，stdin/stdout）
              ↕
        本地 Agent（我们）

    **我们不碰 Frontier，只实现 ACP。**
    这样完全合规、跟 coze-bridge 升级不冲突。

## ⚠️ stdout 只能走协议帧

    ACP 用 stdout 传 JSON-RPC。
    任何一个 print() 打到 stdout 都会污染协议，
    客户端那边看到的就是"解析失败"。

    所以这个文件里：
        · 所有日志走 sys.stderr
        · 模块导入时就校验一遍

## ⚠️ 为什么不实现 fs/read_text_file 这些

    ACP 允许 Agent 让 Client 帮忙读写文件。
    但 **atrium 的前台不需要文件** ——
    它只派活，干活是引擎的事。

    少一个能力就少一个被注入的面。
"""

from __future__ import annotations

import json
import os
import sys
import traceback

# ⚠️ 保住 stdout 原样，所有普通输出重定向到 stderr。
_REAL_STDOUT = None


def _setup_streams() -> None:
    """把 stdout 让给协议，日志走 stderr。"""
    global _REAL_STDOUT

    # 强制 UTF-8 —— Windows 上默认 GBK，中文全乱
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    _REAL_STDOUT = sys.stdout
    sys.stdout = sys.stderr


def _send(obj: dict) -> None:
    """发一帧。"""
    line = json.dumps(obj, ensure_ascii=False)
    out = _REAL_STDOUT or sys.__stdout__
    try:
        buf = getattr(out, "buffer", None)
        if buf is not None:
            buf.write((line + "\n").encode("utf-8"))
            buf.flush()
        else:
            out.write(line + "\n")
            out.flush()
    except Exception:  # noqa: BLE001
        pass


def _notify(session_id: str, update: dict) -> None:
    """发一条 session/update 通知。"""
    _send({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": session_id, "update": update},
    })


def _chunk(session_id: str, text: str, msg_id: str = "m1") -> None:
    """发一段模型输出。"""
    if not text:
        return
    _notify(session_id, {
        "sessionUpdate": "agent_message_chunk",
        "messageId": msg_id,
        "content": {"type": "text", "text": text},
    })


def _reply(req_id, result: dict = None, error: dict = None) -> None:
    """回一帧。"""
    msg = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result if result is not None else {}
    _send(msg)


# ══════════════════════════════════════════════════════
# 会话
# ══════════════════════════════════════════════════════

class Session:
    __slots__ = ("sid", "cwd", "cancelled", "n")

    def __init__(self, sid: str, cwd: str) -> None:
        self.sid = sid
        self.cwd = cwd
        self.cancelled = False
        self.n = 0


_sessions: dict[str, Session] = {}
_seq = 0


def _new_session_id() -> str:
    global _seq

    import uuid

    _seq += 1
    return f"sess_{_seq}_{uuid.uuid4().hex[:6]}"


# ══════════════════════════════════════════════════════
# 剥扣子的元信息
# ══════════════════════════════════════════════════════

def strip_coze_context(text: str) -> tuple[str, dict]:
    """把扣子注入的 <coze-context> 剥出来。

    ## 为什么

        扣子发来的 prompt 长这样：

            <coze-context>
            单聊消息
            account_id: 0
            sender_user_id: 32473321
            </coze-context>
            你好呀

        <coze-context> 是**给程序的元信息**，不是用户说的话。

        不剥的话，模型会把 "account_id: 0" 当成任务内容，
        回复里一半在讲环境。

        实测（2026-11-04）：扣子里看到的回复，
        开头三句都在说"环境是 Windows 命令行"。

    ## 返回

        (剩下的真正任务, 元信息字典)
    """
    import re as _re

    meta: dict = {}
    m = _re.search(r"<coze-context>([\s\S]*?)</coze-context>", text)
    if m:
        body = m.group(1)
        for line in body.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                k = k.strip()
                v = v.strip()
                if k and v and len(k) < 40:
                    meta[k] = v
        text = text[: m.start()] + text[m.end():]

    return text.strip(), meta


def _clean_surrogates(s: str) -> str:
    """清掉孤立的代理对。

    ⚠️ 平台的消息里可能带 \udc80 这种孤立代理对
       （UTF-8 被当成 UTF-16 读过的产物）。
       它会一路带到模型请求体里，
       在 json.dumps(...).encode("utf-8") 那一步炸掉。

       实测（2026-11-04）：扣子里看到的就是一句
       UnicodeEncodeError，任务根本没跑起来。
    """
    try:
        return s.encode("utf-8", "replace").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return s


# ══════════════════════════════════════════════════════
# 方法实现
# ══════════════════════════════════════════════════════

def m_initialize(params: dict):
    return {
        "protocolVersion": 1,
        "agentInfo": {
            "name": "atrium",
            "version": _version(),
            "title": "atrium 前台",
        },
        "agentCapabilities": {
            # ⚠️ 不声明 fs 能力：前台不需要文件，
            #    少一个能力就少一个被注入的面。
            "loadSession": False,
            "requestPermission": False,
        },
    }


def m_new_session(params: dict):
    cwd = params.get("cwd") or os.getcwd()
    sid = _new_session_id()
    _sessions[sid] = Session(sid, cwd)
    print(f"[atrium-acp] 新会话 {sid}  cwd={cwd}", file=sys.stderr)
    return {"sessionId": sid}


def m_cancel(params: dict) -> None:
    """取消。⚠️ 通知，永不回复。"""
    sid = params.get("sessionId") or ""
    s = _sessions.get(sid)
    if s is not None:
        s.cancelled = True
        print(f"[atrium-acp] 取消 {sid}", file=sys.stderr)


def m_prompt(params: dict) -> dict:
    sid = params.get("sessionId") or ""
    sess = _sessions.get(sid)
    if sess is None:
        # ⚠️ 没建会话就来 prompt —— 临时建一个，别报错。
        sess = Session(sid or _new_session_id(), "")
        _sessions[sess.sid] = sess
        sid = sess.sid

    sess.cancelled = False
    sess.n += 1

    # ── 取用户说的话 ──
    blocks = params.get("prompt") or []
    parts: list[str] = []
    if isinstance(blocks, list):
        for b in blocks:
            if not isinstance(b, dict):
                parts.append(str(b))
                continue
            t = b.get("type")
            if t == "text":
                parts.append(str(b.get("text") or ""))
            elif t == "resource":
                r = b.get("resource") or {}
                parts.append(str(r.get("text") or ""))
            elif t == "resource_link":
                parts.append(f"[{b.get('name') or b.get('uri') or '文件'}]")
    elif isinstance(blocks, str):
        parts.append(blocks)

    task = "\n".join(p for p in parts if p).strip()

    # ⚠️ 收到就洗，见 _clean_surrogates 的说明
    task = _clean_surrogates(task)

    # ⚠️ 剥掉扣子注入的元信息 —— 那不是用户说的话
    task, _meta = strip_coze_context(task)

    if not task:
        _chunk(sid, "（没收到内容）")
        return {"stopReason": "end_turn"}

    print(f"[atrium-acp] 开跑：{task[:60]}", file=sys.stderr)

    # ── 跑前台 ──
    try:
        front = _get_front()
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        _chunk(sid, f"前台起不来：{type(e).__name__}: {e}")
        return {"stopReason": "refusal"}

    if front is None:
        _chunk(sid, "前台没配好。检查一下配置文件（~/.atrium/config.json）。")
        return {"stopReason": "refusal"}

    try:
        text = front.reply(task)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        _chunk(sid, f"前台出错：{type(e).__name__}: {e}")
        return {"stopReason": "refusal"}

    if sess.cancelled:
        return {"stopReason": "cancelled"}

    _chunk(sid, text or "（没有输出）")
    return {"stopReason": "end_turn"}


# ══════════════════════════════════════════════════════
# 前台实例（懒加载，整个进程一份）
# ══════════════════════════════════════════════════════

_front = None


def _get_front():
    """拿前台 Agent。第一次调用时造，之后复用。"""
    global _front
    if _front is not None:
        return _front

    from atrium.core.app import Atrium, load_config

    cfg = load_config()
    if not cfg:
        return None

    app = Atrium(cfg)
    _front = app._front
    return _front


def _version() -> str:
    try:
        from atrium import __version__

        return __version__
    except Exception:  # noqa: BLE001
        return "0.1.0"


# ══════════════════════════════════════════════════════
# 主循环
# ══════════════════════════════════════════════════════

METHODS = {
    "initialize": m_initialize,
    "session/new": m_new_session,
    "session/prompt": m_prompt,
    "session/cancel": m_cancel,
}


def main() -> int:
    _setup_streams()
    print("[atrium-acp] 启动", file=sys.stderr)

    stdin_bin = getattr(sys.stdin, "buffer", None) or sys.stdin

    while True:
        raw = stdin_bin.readline()
        if not raw:
            break
        if isinstance(raw, bytes):
            line = raw.decode("utf-8", "replace").strip()
        else:
            line = str(raw).strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(f"[atrium-acp] 非 JSON：{line[:100]}", file=sys.stderr)
            continue

        if not isinstance(msg, dict):
            continue

        method = msg.get("method") or ""
        req_id = msg.get("id")
        params = msg.get("params") or {}

        # ⚠️ 通知（没有 id）永不回复。
        is_notification = "id" not in msg or req_id is None

        print(f"[atrium-acp] ← {method}", file=sys.stderr)

        fn = METHODS.get(method)
        if fn is None:
            if not is_notification:
                _reply(req_id, error={
                    "code": -32601,
                    "message": f"没有这个方法：{method}",
                })
            continue

        try:
            result = fn(params)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            if not is_notification:
                _reply(req_id, error={
                    "code": -32603,
                    "message": f"{type(e).__name__}: {e}",
                })
            continue

        if is_notification:
            continue

        if result is None:
            result = {}
        _reply(req_id, result=result)

    print("[atrium-acp] stdin 关了，退出", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
