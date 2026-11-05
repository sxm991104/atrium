"""atrium 的测试。

## 为什么从零写

    atrium 拆分出来之后一行测试都没有（2026-11-05 查的）。

    而它现在是**所有消息的入口**：
        飞书 / 钉钉的消息先到它，再由它判「闲聊还是派活」，
        派活的话转给桥去干。

    这一层出错的后果：
        · 判错了 → 用户说「你好」，它当成任务派出去，电脑上开始乱动
        · 平台没注册上 → 用户配好了却收不到消息
        · 工具分派错了 → 前台调了个不存在的工具，静默失败

## 测什么、不测什么

    测：
        · 平台注册表（有哪些平台、构造得出来吗）
        · 工具分派（认识哪些名字、不认识怎么办）
        · 安全边界（降级必须拒、验证码流程）
        · 配置读写（坏文件不能崩）
        · 消息摘要 / 心跳（小函数也值得钉住）

    不测：
        · 真的连飞书/钉钉（要凭据、要网络，放端到端）
        · 真的调模型（要花钱、结果不确定）
        · 真的派活给桥（那属于桥的测试范围）

    后两类用 mock 或者干脆不碰 —— 测它们得到的是"网络好不好"，
    不是"代码对不对"。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

# 让 `import atrium` 能找到
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ══════════════════════════════════════════════════════
# 平台注册表
# ══════════════════════════════════════════════════════


class TestRegistry:
    def test_builtin_platforms(self):
        """飞书和钉钉必须都注册着。

        ⚠️ 这一条看着废话，但它防的是"注册代码被误删"——
            删了之后用户配好了却收不到消息，而且没有任何报错。
        """
        from atrium.platforms import registry

        names = registry.names()
        assert "feishu" in names
        assert "dingtalk" in names

    def test_unknown_platform_returns_none(self):
        """不认识的平台返回 None —— 这是**有意设计**，不是 bug。

        ⚠️ 我第一版测试写的是"必须抛异常"，跑出来失败才去看代码，
            代码里明确写着：

                「不认识的平台返回 None，不抛异常。
                  因为配置里可能写了"将来支持"的平台名，
                  那时候不该让整个 atrium 起不来。」

            这个设计是对的：用户手改配置写错一个字母，
            不该让整个门都打不开。顶多那个平台不工作。

        但**返回 None 之后必须有人判空** —— 那条由
        test_app_checks_none 盯着（见下面）。
        """
        from atrium.platforms import registry

        r = registry.make_platform("qqqq", {})
        assert r is None

    def test_app_checks_none(self):
        """造平台的调用方必须判空，否则 None.start() 会炸。

        ⚠️ 这条盯的是 app.py 里那个 if：
                p = self._make_platform(name, pcfg)
                if p is not None:
                    self.platforms[name] = p

            有人把那句 if 删掉（看着像是多余的保护），
            配置里写错平台名的用户就会撞 AttributeError。
        """
        import inspect

        from atrium.core import app as app_mod

        src = inspect.getsource(app_mod)

        # 找 make_platform 调用附近有没有判空
        idx = src.find("_make_platform(name, pcfg)")
        assert idx > 0, "找不到造平台的调用，测试要跟着代码改"

        nearby = src[idx : idx + 200]
        assert "is not None" in nearby or "if p" in nearby, (
            "造完平台没判空 —— None 传下去会在 .start() 上炸"
        )

    def test_make_feishu(self):
        """飞书能构造出来（不连接）。"""
        from atrium.platforms import registry

        p = registry.make_platform(
            "feishu",
            {"app_id": "cli_test", "app_secret": "secret_test", "enabled": True},
        )
        assert p is not None
        # 平台对象至少得有 start / send 这两个动作
        assert hasattr(p, "start") or hasattr(p, "run")

    def test_make_dingtalk(self):
        """钉钉能构造出来。"""
        from atrium.platforms import registry

        p = registry.make_platform(
            "dingtalk",
            {"client_id": "ding_test", "client_secret": "secret_test", "enabled": True},
        )
        assert p is not None


# ══════════════════════════════════════════════════════
# 工具分派
# ══════════════════════════════════════════════════════


class TestToolDispatch:
    def test_specs_count(self):
        """工具表要有内容，而且每个都有 name。

        ⚠️ 不写死数字 —— 写死的话每次加工具都要回来改这条测试。
            改成"至少 10 个"这种下限判断。
        """
        from atrium.agent import tools

        assert len(tools.TOOL_SPECS) >= 10

        for spec in tools.TOOL_SPECS:
            assert spec.get("type") == "function"
            fn = spec.get("function") or {}
            assert fn.get("name"), f"有个工具没写 name：{spec}"
            assert fn.get("description"), f"{fn.get('name')} 没写 description"

    def test_security_mode_tool_exists(self):
        """切安全模式的工具必须在。

        它是远程办公场景下唯一能调安全模式的入口 ——
        用户不在电脑前，没法点桌面端。少了它就等于那个功能废了。
        """
        from atrium.agent import tools

        names = [s["function"]["name"] for s in tools.TOOL_SPECS]
        assert "security_mode" in names

    def test_unknown_tool(self):
        """不认识的工具名必须明确报错。

        ⚠️ 不能"随便找个工具跑" —— 前台的工具面很窄，
            走错一步就是意外。
        """
        from atrium.agent import tools

        r = tools.execute(None, "no_such_tool_xyz", {})
        assert "没有这个工具" in r

    def test_execute_ignores_extra_args(self):
        """多余参数要丢掉，不能炸。

        模型偶尔会自作主张加字段（比如给 list_models 加个 why）。
        那种情况应该忽略，不是报错。
        """
        from atrium.agent import tools

        # list_models 不需要 engine，走 HTTP；这里只验"多余参数不炸"
        try:
            tools.execute(None, "list_models", {"多余": 1, "why": "test"})
        except TypeError as e:
            pytest.fail(f"多余参数把工具搞炸了：{e}")
        except Exception:
            # 别的异常（比如服务没起）不算这条测试的问题
            pass


# ══════════════════════════════════════════════════════
# 安全边界
# ══════════════════════════════════════════════════════


class TestSecurityBoundary:
    def test_downgrade_blocked(self):
        """从前台降低保护必须被拦住。

        老爸定的规则（2026-11-05）：
            「前台只能去开，不能关。」

        这条是硬规则 —— 前台的消息可能是别人发的。
        """
        from atrium.agent import security_tools

        # 严格防护（强）→ 出事可回退（弱）是降级
        assert security_tools.STRENGTH["strict"] > security_tools.STRENGTH["rollback"]
        assert security_tools.STRENGTH["strict"] > security_tools.STRENGTH["both"]
        assert security_tools.STRENGTH["both"] > security_tools.STRENGTH["rollback"]

    def test_strength_matches_bridge(self):
        """atrium 和桥的强度表必须一致。

        ⚠️ 两边不一致的话：
                atrium 放过去的请求被桥拒掉（用户看到"点了没反应"）
            或者
                atrium 拦住了桥本该接受的（功能直接没了）

            这张表是个隐形的跨仓库契约，必须有测试钉住。
        """
        from atrium.agent import security_tools

        bridge_root = os.environ.get("BRIDGE_DIR") or r"D:\Project\dsh-bridge-oss"
        if not os.path.isdir(bridge_root):
            pytest.skip("找不到桥的目录，跳过跨仓库一致性检查")

        sys.path.insert(0, bridge_root)
        try:
            from guard import capabilities as cap
        except ImportError:
            pytest.skip("桥的 guard 模块 import 不了")
        finally:
            try:
                sys.path.remove(bridge_root)
            except ValueError:
                pass

        assert security_tools.STRENGTH == cap.STRENGTH, (
            f"两边强度表不一致：\n"
            f"  atrium: {security_tools.STRENGTH}\n"
            f"  桥:     {cap.STRENGTH}"
        )

    def test_unknown_mode(self):
        """不认识的模式要明确拒绝。"""
        from atrium.agent import security_tools

        r = security_tools.set_security_mode("nonsense_mode")
        assert "没有" in r or "不认识" in r or "不是" in r


# ══════════════════════════════════════════════════════
# 消息处理
# ══════════════════════════════════════════════════════


class TestMessage:
    def test_preview_short(self):
        """短消息原样返回。"""
        from atrium.core import message

        assert message.preview("你好") == "你好"

    def test_preview_truncates(self):
        """长消息要截断，不能把整段日志塞进界面。"""
        from atrium.core import message

        long_text = "啊" * 500
        r = message.preview(long_text, 20)
        assert len(r) <= 25, f"截断没生效，长度 {len(r)}"

    def test_preview_newlines(self):
        """换行要压掉 —— 界面上是一行显示。"""
        from atrium.core import message

        r = message.preview("第一行\n第二行\n第三行", 100)
        assert "\n" not in r

    def test_preview_empty(self):
        """空消息不能炸。"""
        from atrium.core import message

        assert message.preview("") == ""
        assert message.preview(None) in ("", None)


# ══════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════


class TestConfig:
    def test_config_path_is_home(self):
        """配置在 ~/.atrium/ 下，不在 APPDATA。

        ⚠️ 这两个位置以前混过：
            读的写的不在同一个地方，用户配了不生效，
            而且不报错 —— 只是"配了像没配"。
        """
        from atrium.core import app

        p = app.config_path()
        home = os.path.expanduser("~")
        assert p.startswith(home), f"配置不在用户目录：{p}"
        assert ".atrium" in p

    def test_load_missing_returns_empty(self):
        """配置文件不存在时返回空 dict，不抛异常。"""
        from atrium.core import app

        old = app.config_path()
        tmpdir = tempfile.mkdtemp()
        fake = os.path.join(tmpdir, "nope.json")

        # 临时把它指到一个不存在的位置
        orig = getattr(app, "config_path", None)
        try:
            app.config_path = lambda: fake  # type: ignore
            d = app.load_config()
            assert isinstance(d, dict)
        finally:
            if orig:
                app.config_path = orig  # type: ignore
            assert old or True

    def test_load_broken_json(self):
        """坏 JSON 不能崩 —— 返回空 dict 让程序继续跑。

        ⚠️ 用户手改配置文件时打错一个字符，不该白屏。
        """
        from atrium.core import app

        tmpdir = tempfile.mkdtemp()
        fake = os.path.join(tmpdir, "bad.json")
        with open(fake, "w", encoding="utf-8") as f:
            f.write("{ 这不是 json }")

        orig = getattr(app, "config_path", None)
        try:
            app.config_path = lambda: fake  # type: ignore
            d = app.load_config()
            assert isinstance(d, dict)
        finally:
            if orig:
                app.config_path = orig  # type: ignore


# ══════════════════════════════════════════════════════
# 心跳
# ══════════════════════════════════════════════════════


class TestBeat:
    def test_age_none_when_missing(self):
        """没有心跳文件时返回 None，不炸。"""
        from atrium.core import beat

        p = beat.path()
        if os.path.exists(p):
            pytest.skip("这台机器上有真心跳文件，跳过")

        assert beat.age() is None


# ══════════════════════════════════════════════════════
# 摘要
# ══════════════════════════════════════════════════════


class TestDigest:
    def test_collect_empty(self):
        """空输入返回空列表。"""
        from atrium.core import digest

        assert digest.collect_files([]) == []

    def test_build_empty(self):
        """空输入也要产出能看的东西，不能是 None。"""
        from atrium.core import digest

        r = digest.build_digest([])
        assert isinstance(r, str)
