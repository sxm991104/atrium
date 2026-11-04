"""给 atrium 的引擎客户端加验证码 / 提权 / 解锁。

## 为什么这几样走 HTTP 而不是命令行

    引擎有两种入口：

        命令行 core/bridge.py    派活、查进度、列任务、取消
        HTTP 127.0.0.1:8790      验证码、提权、解锁、权限查询

    为什么不全走一个：

        命令行更适合"一次性动作"（派活、查状态），
        不依赖那个服务在跑，起得来就能用。

        而验证码这套是**有状态的**：
            出码 → 邮件 → 用户填码 → 校验 → 放行
        状态在 guard 模块里，还有个验证码计数。
        这些接口只有 HTTP 那边暴露了，命令行没做。

## ⚠️ 前提：8790 那个服务要在跑

    它是桌面端起的时候拉起来的。
    没在跑的话，验证码这条路走不通 ——
    但派活查进度还能用（走命令行）。

    所以这里每次调用都判断一下，失败了给个能看懂的原因，
    不是直接抛一堆 urllib 的报错。

## ⚠️ token 从哪来

    guard/security.py::token_path() 给出位置。
    实机上是 %APPDATA%/dsh-bridge/api_token.txt（Linux 上在 XDG 配置目录）。

    这里直接按同一个规则找，不 import 引擎的模块 ——
    atrium 不该依赖 remote-dispatch 的代码结构，只依赖它的接口。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

#: 默认端口。引擎那边可以用 DSH_BRIDGE_PORT 改，这里跟着读。
_DEFAULT_PORT = 8790


def _port() -> int:
    try:
        return int(os.environ.get("DSH_BRIDGE_PORT") or _DEFAULT_PORT)
    except (TypeError, ValueError):
        return _DEFAULT_PORT


def _token_paths() -> list[str]:
    """api_token.txt 可能在哪。

    ⚠️ 列几个位置而不是写死一个：
        Windows 在 %APPDATA%，Linux 在 XDG 配置目录，
        用户还可能用环境变量改过。
    """
    out = []

    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(os.path.join(appdata, "dsh-bridge", "api_token.txt"))

    home = os.path.expanduser("~")
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    out.append(os.path.join(xdg, "dsh-bridge", "api_token.txt"))

    # 兜底：老版本可能放在这儿
    out.append(os.path.join(home, ".dsh-bridge", "api_token.txt"))

    return out


def _read_token() -> str:
    """读访问令牌。读不到返回空串。"""
    for p in _token_paths():
        try:
            with open(p, encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                return t
        except OSError:
            continue
    return ""


class ApiError(Exception):
    """调引擎 HTTP 接口失败。

    ⚠️ 单独一个异常类型，因为调用方要区分：
        "服务没起来"和"验证码错了"是完全不同的两件事，
        给用户的提示也不一样。
    """


def call(path: str, body: dict | None = None, timeout: float = 20.0) -> dict:
    """调一次引擎的 HTTP 接口。

    path 比如 "/api/guard/verify"。
    body 是 None 就发 GET，否则发 POST。
    """
    url = f"http://127.0.0.1:{_port()}{path}"

    headers = {}
    tok = _read_token()
    if tok:
        # ⚠️ 引擎要的是 X-Token 头，不是 Authorization: Bearer。
        #
        #    实测（2026-11-04）：
        #        带 Authorization 过去，回的是
        #        {"error":"bad or missing X-Token"} —— 401。
        #
        #    X-Token 这个名字是从 server/api_server.py 里读出来的，
        #    不是猜的。
        headers["X-Token"] = tok

    data = None
    method = "GET"
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        method = "POST"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        # ⚠️ HTTPError 里也有 body，引擎出错时会写原因。
        #    直接抛 e 的话原因就丢了。
        try:
            d = json.loads(e.read())
            if isinstance(d, dict):
                msg = d.get("error") or d.get("message") or ""
                raise ApiError(msg or f"HTTP {e.code}") from e
        except (json.JSONDecodeError, OSError):
            pass
        raise ApiError(f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        # 连不上 —— 服务没起来
        raise ApiError(
            "连不上引擎（127.0.0.1:%d）。桌面端起没起？" % _port()
        ) from e
    except OSError as e:
        raise ApiError(f"{type(e).__name__}: {e}") from e

    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ApiError("引擎返回的不是 JSON") from e

    # ⚠️ 引擎有的接口返回**数组**（比如 /api/tasks 直接给任务列表），
    #    不能一律要求是对象。
    #
    #    实测（2026-11-04）：加了"必须是对象"的判断之后，
    #    /api/tasks 直接被判成失败。
    if isinstance(d, list):
        return d

    if not isinstance(d, dict):
        raise ApiError("引擎返回的不是对象也不是数组")
    return d
