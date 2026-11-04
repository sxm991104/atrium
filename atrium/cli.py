"""atrium 命令行。

    python -m atrium.cli init        生成配置模板
    python -m atrium.cli run         启动
    python -m atrium.cli status      看状态
    python -m atrium.cli test        测配置（能不能换到 token）
"""

from __future__ import annotations

import json
import os
import sys

from atrium.core.app import config_path, load_config, run, save_config

DEFAULT = {
    "engine": {
        "type": "remote-dispatch",
        "path": "D:/Project/dsh-bridge-oss",
    },
    "storage": {
        "dir": "",
    },
    "platforms": {
        "feishu": {
            "enabled": False,
            "app_id": "",
            "app_secret": "",
            "verify_ssl": True,
        },
    },
}


def cmd_init() -> int:
    p = config_path()
    if os.path.exists(p):
        print(f"  配置已存在：{p}")
        print("  要重来就先删掉它。")
        return 0

    save_config(DEFAULT)
    print(f"  ✅ 生成了 {p}")
    print()
    print("  接下来：")
    print("    1. 打开它，填 feishu 的 app_id / app_secret")
    print("    2. 把 platforms.feishu.enabled 改成 true")
    print("    3. 跑：python -m atrium.cli test")
    print("    4. 跑：python -m atrium.cli run")
    return 0


def cmd_run() -> int:
    return run()


def cmd_status() -> int:
    cfg = load_config()
    if not cfg:
        print("  没有配置。先跑 init。")
        return 1

    print("=" * 60)
    print("  atrium 配置")
    print("=" * 60)
    print(f"  配置文件：{config_path()}")

    eng = cfg.get("engine") or {}
    print(f"  引擎：    {eng.get('type')}  {eng.get('path')}")
    ep = eng.get("path") or ""
    if ep:
        ok = os.path.exists(os.path.join(ep, "core", "bridge.py"))
        print(f"             {'✅ 找得到' if ok else '❌ 找不到 core/bridge.py'}")

    print()
    print("  平台：")
    for name, pcfg in (cfg.get("platforms") or {}).items():
        en = "✅ 开" if pcfg.get("enabled") else "· 关"
        print(f"    {name:10} {en}")

    print()
    print("  队列：")
    from atrium.core.queue import Bus

    bus = Bus((cfg.get("storage") or {}).get("dir") or "")
    print(f"    inbox   {len(bus.inbox)} 条待处理")
    print(f"    outbox  {len(bus.outbox)} 条待发送")
    print(f"    done    {len(bus.done)} 条已处理")
    print(f"    failed  {len(bus.failed)} 条失败")
    print(f"    目录    {bus.dir}")
    return 0


def cmd_test() -> int:
    """测配置能不能用。

    ## ⚠️ 这一块原来只认飞书的 _get_token

        飞书后来改用 lark-oapi（SDK 自己管 token，
        我们不碰），结果这里调 _get_token 直接 AttributeError。

        钉钉更惨 —— 压根没测，那一节是空的。

    ## 现在怎么做

        **不猜每个平台内部怎么实现的**，只调它自己的 health()。

        每个平台实现自己的 health()，说清楚"我配好了没"。
        cli 不认识任何具体平台 —— 加新平台不用改这里。

        （这跟 platforms/registry.py 是一个思路：
          主程序不该知道具体平台的内部结构。）
    """
    cfg = load_config()
    if not cfg:
        print("  没有配置。先跑 init。")
        return 1

    ok_all = True

    # ── 引擎 ──
    print("=" * 60)
    print("  引擎")
    print("=" * 60)
    try:
        from atrium.engine.client import make_engine

        eng = make_engine(cfg.get("engine") or {})
        tasks = eng.list_tasks(3)
        print(f"  ✅ 能调通，最近 {len(tasks)} 个任务")
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ {type(e).__name__}: {e}")
        ok_all = False

    # ── 各平台 ──
    #
    # ⚠️ 这里不走 registry.make_platform —— 那个会连平台。
    #    test 只想知道"配得对不对"，不该真的建立长连接。
    from atrium.platforms import registry

    for name, pcfg in (cfg.get("platforms") or {}).items():
        if not pcfg.get("enabled"):
            continue

        print()
        print("=" * 60)
        print(f"  平台：{name}")
        print("=" * 60)

        p = registry.make_platform(name, pcfg)
        if p is None:
            print(f"  ❌ 没有这个平台（已支持：{', '.join(registry.names())}）")
            ok_all = False
            continue

        try:
            h = p.health() or {}
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ health() 出错：{type(e).__name__}: {e}")
            ok_all = False
            continue

        # 平台自己说配好没
        ready = h.get("ready")
        why = str(h.get("why") or "").strip()

        if ready is None:
            # 老平台没实现 ready，只说"构造成功了"
            print("  ✅ 配置能读进来（这个平台没有自检）")
        elif ready:
            print("  ✅ 配置正常")
            if why:
                print(f"     {why}")
        else:
            print(f"  ❌ {why or '配置不完整'}")
            ok_all = False

    print()
    print("=" * 60)
    print(f"  {'✅ 都通了' if ok_all else '❌ 有没通的'}")
    print("=" * 60)
    return 0 if ok_all else 1

def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""

    if cmd == "init":
        return cmd_init()
    if cmd == "run":
        return cmd_run()
    if cmd == "status":
        return cmd_status()
    if cmd == "test":
        return cmd_test()

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
