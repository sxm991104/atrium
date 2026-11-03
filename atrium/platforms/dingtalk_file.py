"""给钉钉平台加发文件能力。

## 钉钉发文件的流程

    一、上传文件拿 media_id

        POST https://oapi.dingtalk.com/media/upload
             ?access_token=<token>&type=file
        multipart/form-data:   media=@文件

        返回 {"errcode":0, "media_id":"@lAxxxxx"}

    ⚠️ 这里用的是**老版 OAPI**（oapi.dingtalk.com），
       不是新版 api.dingtalk.com —— 新版没有这个接口。

    ⚠️ access_token 也是老版的：
        https://oapi.dingtalk.com/gettoken?appkey=..&appsecret=..

    二、用 sessionWebhook 发 file 消息

        POST <sessionWebhook>
        {"msgtype": "file", "file": {"media_id": "@lAxxxxx"}}

## 为什么要这一步

    现在的推送只列文件名：

        ✅ 文档DS（10 秒）· 派发测试.docx(34KB)

    用户看到"派发测试.docx"，但**点不开、拿不到**。

    真正的交付是**把文件本身发过去**，用户点一下就能下载。

## ⚠️ 大小限制

    钉钉 media 上传有大小上限（一般 20MB）。
    超过的跳过，只在文字里提一句文件名。

## ⚠️ multipart 手写

    不用 requests（atrium 现在没这个依赖）。
    用 urllib + 手写 multipart body。
"""

from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

from atrium.platforms.base import PlatformError

#: 上传的大小上限（字节）
_MAX_BYTES = 20 * 1024 * 1024


def _get_oapi_token(app_key: str, app_secret: str, ctx) -> str:
    """换老版 access_token（发文件要用这个）。"""
    q = urllib.parse.urlencode({"appkey": app_key, "appsecret": app_secret})
    url = f"https://oapi.dingtalk.com/gettoken?{q}"

    try:
        with urllib.request.urlopen(url, timeout=20, context=ctx) as r:
            d = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        raise PlatformError(f"换 OAPI token 失败：{e}") from e

    tok = d.get("access_token") or ""
    if d.get("errcode") != 0 or not tok:
        raise PlatformError(
            f"换 OAPI token 失败：errcode={d.get('errcode')} {d.get('errmsg')}"
        )
    return tok


def upload_file(path: str, app_key: str, app_secret: str, ctx) -> str:
    """上传一个文件到钉钉，返回 media_id。

    失败抛 PlatformError。
    """
    if not os.path.isfile(path):
        raise PlatformError(f"文件不存在：{path}")

    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise PlatformError(f"读不到文件大小：{e}") from e

    if size > _MAX_BYTES:
        raise PlatformError(
            f"文件太大（{size // 1024 // 1024}MB > 20MB），钉钉不收"
        )

    tok = _get_oapi_token(app_key, app_secret, ctx)

    # ── 手写 multipart ──
    name = os.path.basename(path)
    boundary = "----atrium" + uuid.uuid4().hex
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"

    try:
        with open(path, "rb") as f:
            content = f.read()
    except OSError as e:
        raise PlatformError(f"读文件失败：{e}") from e

    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="media"; filename="{name}"\r\n'
        f"Content-Type: {mime}\r\n"
        f"\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = head + content + tail

    q = urllib.parse.urlencode({"access_token": tok, "type": "file"})
    url = f"https://oapi.dingtalk.com/media/upload?{q}"

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise PlatformError(f"上传 HTTP {e.code}") from e
    except Exception as e:  # noqa: BLE001
        raise PlatformError(f"上传失败：{type(e).__name__}: {e}") from e

    media_id = d.get("media_id") or ""
    if d.get("errcode") != 0 or not media_id:
        raise PlatformError(
            f"上传失败：errcode={d.get('errcode')} {d.get('errmsg')}"
        )

    return media_id
