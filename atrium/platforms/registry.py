"""平台注册表 —— 加一个平台不用改这里以外的任何地方。

## 为什么要这一层

    之前 app.py 里是写死的：

        if name == "feishu":
            return Feishu(...)
        if name == "dingtalk":
            return DingTalk(...)      ← 每加一个就多一个 if

    这样加平台的正确做法是"改 app.py"，
    而 app.py 是主程序 —— 不该为加平台而改。

## 现在的做法

    平台自己往注册表里登记。加平台 = 加一个文件 + 一行导入。

        platforms/dingtalk.py 里：
            @register("dingtalk")
            def make(cfg): return DingTalk(...)

        platforms/__init__.py 里：
            from atrium.platforms import dingtalk  # noqa: F401

    主程序只调 make_platform(name, cfg)，不认识具体平台。
"""

from __future__ import annotations

from typing import Callable

from atrium.platforms.base import Platform, PlatformError

#: 名字 → 构造函数
_REGISTRY: dict[str, Callable[[dict], Platform]] = {}


def register(name: str):
    """装饰器：把一个平台的构造函数登记进去。

    被登记的构造函数接收配置 dict，返回一个 Platform。
    """
    def deco(fn: Callable[[dict], Platform]) -> Callable[[dict], Platform]:
        _REGISTRY[name] = fn
        return fn

    return deco


def names() -> list[str]:
    """已经登记的平台。"""
    return sorted(_REGISTRY)


def make_platform(name: str, cfg: dict) -> Platform | None:
    """按名字造一个平台。

    ⚠️ 不认识的平台返回 None，不抛异常。
       因为配置里可能写了"将来支持"的平台名，
       那时候不该让整个 atrium 起不来。
    """
    fn = _REGISTRY.get(name)
    if fn is None:
        return None

    try:
        return fn(cfg or {})
    except PlatformError as e:
        print(f"[atrium] {name} 起不来：{e}", flush=True)
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[atrium] {name} 意外出错：{type(e).__name__}: {e}", flush=True)
        return None
