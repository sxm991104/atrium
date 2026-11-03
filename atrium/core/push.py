"""主动推送 —— 任务跑完了，主动告诉用户。

## 为什么需要这个

    ACP 是"一问一答"：
        用户说话 → 我们答 → 结束
        **过了这一轮就断了**

    所以任务跑完之后，没地方说话。
    实测（2026-11-04）：试过 4 种办法都不行：

        · 不带 _meta 主动发 session/update   → bridge 丢掉
        · 带 cozeAgentId / cozeReplySeqID    → 还是丢掉
        · IPC 的 /agent/send                 → 只能发文件
        · 上游 API                           → 没有发消息的接口

## 正确做法：用扣子的触发器

    扣子的智能体可以加「事件触发器」：

        我们 POST 一个 Webhook
            ↓
        扣子的智能体被唤起
            ↓
        **它主动给用户发消息**

    文档：https://docs.coze.cn/guides_task

## ⚠️ 只在飞书渠道生效

    文档原话：
        "触发器功能仅对飞书渠道生效，
         只有将智能体发布到飞书渠道，才可以自动执行触发器的任务。"

    所以这条路要求：智能体发布到**飞书渠道**。

## 配置

    ~/.atrium/config.json 里加：

        "push": {
            "webhook": "https://api.coze.cn/...",
            "token": "ABCxxx",
            "params": {}
        }

    params 是给触发器的请求参数。
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request


class PushError(Exception):
    """推送失败。"""


def _ctx() -> ssl.SSLContext:
    """TLS 上下文。

    ⚠️ 本机有 Steam++ 做中间人，证书吊销检查过不去。
       所以这里默认不校验证书 —— 但这是配置里的开关，
       产品环境应该打开。
    """
    c = ssl.create_default_context()
    if os.environ.get("ATM_INSECURE_TLS") == "1":
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
    return c


def push(cfg: dict, text: str, extra: dict | None = None) -> bool:
    """推一条消息。

    cfg 是 config.json 里的 push 段：
        {"webhook": "...", "token": "...", "params": {...}}

    text 会被放进 params 的 message 字段 ——
    触发器那边要有个能接这个的变量。

    返回 True 表示发出去了。
    """
    webhook = (cfg or {}).get("webhook") or ""
    token = (cfg or {}).get("token") or ""

    if not webhook:
        raise PushError("没配 webhook。见文件头的说明。")

    body = dict((cfg or {}).get("params") or {})
    body["message"] = text
    if extra:
        body.update(extra)

    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(webhook, data=data, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ctx()) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise PushError(f"HTTP {e.code}: {e.read()[:200]!r}") from e
    except Exception as e:  # noqa: BLE001
        raise PushError(f"{type(e).__name__}: {e}") from e

    # 扣子的响应里 BaseResp.StatusCode == 0 表示成功
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return True

    if not isinstance(d, dict):
        return True

    code = None
    br = d.get("BaseResp") or d.get("base_resp") or {}
    if isinstance(br, dict):
        code = br.get("StatusCode", br.get("status_code"))

    if code in (0, None):
        return True

    msg = br.get("StatusMessage") or br.get("status_message") or ""
    raise PushError(f"StatusCode={code} {msg}")


def make_cfg(cfg_path: str = "") -> dict:
    """从配置文件里拿 push 段。"""
    from atrium.core.app import load_config

    cfg = load_config(cfg_path)
    return cfg.get("push") or {}
