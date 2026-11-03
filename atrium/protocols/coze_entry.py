"""atrium 的 coze 入口 —— 对 coze-bridge 冒充 `openclaw`。

## 为什么要冒充

    coze-bridge 只认四种 Agent：

        claude-code | codex | openclaw | hermes-acp

    它没有"自定义 Agent"这个入口。

    实测（2026-11-04）：
        直接让 openclaw.cmd 起 ACP 服务端 →
        bridge 报 [Error] openclaw default agent 未探测到 (code: -32021)

    因为它在起我们之前会先探测，走的是 openclaw 的命令行接口：

        openclaw --log-level silent --version
        openclaw --log-level silent agents list --json
        openclaw --log-level silent gateway status --json --require-rpc
        openclaw models list --json --log-level silent
        openclaw acp --session agent:main:<id>

    **最后那条才是真正的 ACP 会话。**
    前四条是探测，得按它的格式回答。

## ⚠️ 这是"冒充"，不是"支持"

    扣子没开放自定义 Agent 入口，
    我们借 openclaw 的名字接进去。

    **这件事要说清楚，不能藏。**

    合规性：openclaw 是我们自己实现的命令（这个名字只是本地的一个
    .cmd 文件），没有冒用别人的二进制。扣子那边看到的是一个叫
    openclaw 的 Agent，行为是我们实现的。

    将来扣子支持自定义入口，就把这层拆掉。

## 探测的期望格式

    `agents list --json` 必须返回 **JSON 数组**，
    里面至少要有一个元素**同时**有：

        isDefault: true
        id:        "..."
        workspace: "..."

    实测教训：少任何一个，bridge 就说"未探测到默认 agent"。
"""

from __future__ import annotations

import json
import os
import sys


def _safe_json(obj) -> str:
    """序列化成一行 JSON，清掉孤立代理对。

    ⚠️ 平台消息里可能带 \\udc80，
       不洗的话 json.dumps(...).encode("utf-8") 会炸。
    """
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return "{}"
    return s.encode("utf-8", "replace").decode("utf-8", "replace")


def _out(s: str) -> None:
    out = sys.stdout
    try:
        buf = getattr(out, "buffer", None)
        if buf is not None:
            buf.write((s + "\n").encode("utf-8"))
            buf.flush()
        else:
            out.write(s + "\n")
            out.flush()
    except Exception:  # noqa: BLE001
        pass


def _trace(argv: list[str]) -> None:
    """把调用记一笔，排查用。"""
    try:
        import time as _t

        d = os.path.join(os.path.expanduser("~"), ".atrium")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "_coze_calls.log"), "a", encoding="utf-8") as f:
            f.write(f"{_t.strftime('%H:%M:%S')}  argv={argv}\n")
    except OSError:
        pass


def _version() -> str:
    try:
        from atrium import __version__

        return __version__
    except Exception:  # noqa: BLE001
        return "0.1.0"


def _default_agent() -> dict:
    """给 bridge 的默认 agent 描述。

    ⚠️ 三个字段缺一不可：isDefault / id / workspace
    """
    ws = os.path.join(os.path.expanduser("~"), ".atrium", "workspace")
    os.makedirs(ws, exist_ok=True)
    return {
        "id": "atrium",
        "name": "atrium",
        "workspace": ws,
        "isDefault": True,
        "default": True,
        "status": "ready",
    }


def _agents_list(argv: list[str]) -> int:
    """openclaw agents list [--json]"""
    agent = _default_agent()
    if "--json" in argv:
        _out(_safe_json([agent]))
    else:
        _out(f"* {agent['id']}  (default, workspace={agent['workspace']})")
    return 0


def _models_list(argv: list[str]) -> int:
    """openclaw models list [--json]"""
    models = [{
        "id": "atrium-default",
        "name": "atrium",
        "isDefault": True,
        "default": True,
    }]
    if "--json" in argv:
        _out(_safe_json(models))
    else:
        _out("atrium-default  (default)")
    return 0


def _gateway_status(argv: list[str]) -> int:
    """openclaw gateway status [--json]"""
    st = {"ok": True, "status": "running", "rpc": True}
    if "--json" in argv:
        _out(_safe_json(st))
    else:
        _out("gateway: running")
    return 0


def _run_acp(argv: list[str]) -> int:
    """走真正的 ACP。

    ⚠️ 这里不自己实现 ACP —— 直接调 atrium/protocols/acp.py 的 main()。
       ACP 服务端只有一个实现，飞书那条路和这条共用同一个前台。
    """
    try:
        from atrium.protocols import acp as _acp
    except Exception as e:  # noqa: BLE001
        print(f"[atrium-coze] 起不来 ACP：{type(e).__name__}: {e}",
              file=sys.stderr)
        return 1

    return _acp.main()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    _trace(args)

    # 统一 UTF-8 —— Windows 上默认 GBK，中文全乱
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    # 去掉 --log-level xxx 这类全局开关，只看子命令
    clean: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--log-level":
            i += 2
            continue
        clean.append(args[i])
        i += 1

    if not clean:
        _out(f"atrium {_version()}")
        return 0

    head = clean[0]

    if head in ("--version", "-v", "version"):
        _out(_version())
        return 0

    if head == "setup":
        _out("atrium: 不用 setup")
        return 0

    if head == "agents":
        sub = clean[1] if len(clean) > 1 else ""
        if sub == "list":
            return _agents_list(clean[2:])
        _out(f"atrium: agents {sub}")
        return 0

    if head == "models":
        return _models_list(clean[1:])

    if head == "gateway":
        return _gateway_status(clean[1:])

    if head == "acp":
        # ⚠️ 优先走 ACP
        return _run_acp(clean[1:])

    # 认不出来的，退回 ACP —— 大多数情况下 bridge 就是想要个会话
    print(f"[atrium-coze] 不认识的命令 {head}，按 ACP 处理", file=sys.stderr)
    return _run_acp(clean)


if __name__ == "__main__":
    sys.exit(main())
