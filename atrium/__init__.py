"""atrium —— 所有平台的门，都开到这儿。

⚠️ 这个文件第一件事是把 stdout/stderr 强制成 UTF-8。

    Windows 上 Python 默认用 cp936（GBK），
    而平台消息是 UTF-8 —— 打日志、读 stdin 全乱。

    实测（2026-11-04）：飞书那边收到的是「鍚姩锛? 涓钩鍙?」，
    就是 UTF-8 的"启动：1 个平台"被按 GBK 读的结果。
"""

from __future__ import annotations

import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

__version__ = "1.0.1"
