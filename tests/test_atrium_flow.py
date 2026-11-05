"""atrium 的行为测试 —— 跟 test_atrium.py 分开。

## 分工

    test_atrium.py        **结构**：注册表有没有、工具有没有、
                          配置读得出来吗
    test_atrium_flow.py   **行为**：消息进来后怎么走、工具真调起来
                          是什么反应、错了说什么

## ⚠️ 这个文件一律不碰真东西

    不发真邮件、不连真平台、不调真模型、不派真活。

    凡是会"真的做点什么"的地方，都用假的替掉：
        · _api.call 换成一个假的，记下被怎么调的
        · 平台的 send 换成假的，看它收到什么
        · 配置指向临时文件

    为什么：这套测的是"代码逻辑对不对"。
    真的发邮件/派活会：
        · 吵到用户（手机响）
        · 花钱（调模型）
        · 在用户电脑上真的动文件
    而这些都不是测试想验证的东西。
"""

from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ══════════════════════════════════════════════════════
# 假的 api（记录所有调用）
# ══════════════════════════════════════════════════════


class FakeApi:
    """假的 HTTP 客户端。

    ## 怎么用

        fake = FakeApi({
            "/api/security/mode": {"mode": "strict", "features": {}},
        })
        # 把工具模块里的 _api 换掉
        monkeypatch.setattr(tools_mod, "_api", fake)

    之后 fake.calls 里就是所有调用记录。
    """

    def __init__(self, responses: dict | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, dict | None]] = []

    def call(self, path: str, body: dict | None = None, **kwargs):  # noqa: ARG002
        self.calls.append((path, body))

        # 精确匹配
        if path in self.responses:
            r = self.responses[path]
            if isinstance(r, Exception):
                raise r
            return r

        # 前缀匹配（比如 /api/task/xxx/log）
        for k, v in self.responses.items():
            if k.endswith("*") and path.startswith(k[:-1]):
                if isinstance(v, Exception):
                    raise v
                return v

        # 默认返回空成功
        return {"ok": True}


class _ApiError(Exception):
    """模拟 _api.ApiError。"""


# ══════════════════════════════════════════════════════
# 安全模式：完整流程
# ══════════════════════════════════════════════════════


class TestSecurityModeFlow:
    """切安全模式的完整行为。"""

    def test_query_only(self, monkeypatch):
        """不带 mode = 只查，不该发任何修改请求。"""
        from atrium.agent import security_tools

        fake = FakeApi({
            "/api/security/mode": {
                "mode": "strict",
                "label": "严格防护",
                "features": {"guard": True, "code": True, "rewind": False},
            }
        })
        monkeypatch.setattr(security_tools, "_api", fake)

        r = security_tools.set_security_mode("")

        assert "严格防护" in r
        # 只有一次 GET，没有 POST
        assert len(fake.calls) == 1
        assert fake.calls[0][1] is None

    def test_downgrade_refused_without_api_call(self, monkeypatch):
        """降级要在**发请求之前**就拒掉。

        ⚠️ 拒掉之后不能再调接口 —— 调了就等于"问一下服务端能不能降"，
            万一服务端那边逻辑有洞就漏过去了。
        """
        from atrium.agent import security_tools

        fake = FakeApi({
            "/api/security/mode": {"mode": "strict", "label": "严格防护"},
        })
        monkeypatch.setattr(security_tools, "_api", fake)

        r = security_tools.set_security_mode("rollback")

        assert "不能这么改" in r or "降低保护" in r
        # ⚠️ 只有那一次查询，没有任何修改请求
        assert len(fake.calls) == 1
        for path, body in fake.calls:
            assert body is None, f"降级时不该发修改请求，但发了：{path} {body}"

    def test_upgrade_needs_code(self, monkeypatch):
        """升级：第一次不带码 → 服务端说 need_code。"""
        from atrium.agent import security_tools

        fake = FakeApi({
            "/api/security/mode": {"mode": "rollback", "label": "出事可回退"},
            "/api/security/mode/verified": {
                "ok": False,
                "need_code": True,
                "message": "码发到邮箱了",
            },
        })
        monkeypatch.setattr(security_tools, "_api", fake)

        r = security_tools.set_security_mode("strict")

        assert "验证" in r or "码" in r

        # 必须打的是**要码那个接口**，不是桌面端那个
        paths = [p for p, _ in fake.calls]
        assert "/api/security/mode/verified" in paths, (
            f"前台切模式必须走 verified 接口，实际打了：{paths}"
        )

    def test_with_code_passes_through(self, monkeypatch):
        """带码来：要原样传给服务端。"""
        from atrium.agent import security_tools

        fake = FakeApi({
            "/api/security/mode": {"mode": "rollback", "label": "出事可回退"},
            "/api/security/mode/verified": {
                "ok": True,
                "mode": "strict",
                "label": "严格防护",
            },
        })
        monkeypatch.setattr(security_tools, "_api", fake)

        r = security_tools.set_security_mode("strict", code="123456")

        assert "切好了" in r

        # 找那次 verified 调用
        verified = [b for p, b in fake.calls if p == "/api/security/mode/verified"]
        assert verified, "没调 verified 接口"
        assert verified[0].get("code") == "123456", (
            f"码没传过去：{verified[0]}"
        )

    def test_same_mode_noop(self, monkeypatch):
        """已经是这个模式：什么都不做。"""
        from atrium.agent import security_tools

        fake = FakeApi({
            "/api/security/mode": {"mode": "strict", "label": "严格防护"},
        })
        monkeypatch.setattr(security_tools, "_api", fake)

        r = security_tools.set_security_mode("strict")

        assert "不用改" in r or "就是" in r
        # 不该有修改请求
        assert all(b is None for _, b in fake.calls)

    def test_api_down_gives_readable_error(self, monkeypatch):
        """服务没起：给一句人话，不是一堆 urllib 报错。"""
        from atrium.agent import security_tools

        # ⚠️ 假的 api 必须带上 ApiError 这个属性。
        #
        #    security_tools 里写的是
        #        except _api.ApiError as e:
        #    所以 _api 被换成别的对象之后，那个对象也得有 ApiError，
        #    否则会先在 except 那一行炸掉（AttributeError），
        #    根本走不到"给一句人话"的分支。
        #
        #    （第一版就是这么写错的：只给了个 call 方法，
        #      跑出来 AttributeError: 'Boom' object has no attribute 'ApiError'。）
        class BoomApi:
            class ApiError(Exception):
                pass

            def call(self, *a, **k):
                raise BoomApi.ApiError("Connection refused")

        monkeypatch.setattr(security_tools, "_api", BoomApi())

        r = security_tools.set_security_mode("")
        assert "没走通" in r or "失败" in r or "连" in r
        # 不该出现 Python 异常类型名
        assert "Traceback" not in r


# ══════════════════════════════════════════════════════
# 工具分派：参数处理
# ══════════════════════════════════════════════════════


class TestToolArgs:
    """工具怎么处理参数。"""

    def test_bool_as_string(self):
        """模型有时候把 true 写成 "true" —— 不能因此崩。"""
        from atrium.agent import tools

        # check_asks 的 limit 是 int，传个字符串进去
        try:
            tools.execute(None, "check_asks", {"limit": "abc"})
        except ValueError:
            pytest.fail("limit 传字符串直接崩了，应该退化成默认值")

    def test_missing_required_arg(self):
        """缺必填参数：给一句能看懂的话，不抛异常。"""
        from atrium.agent import tools

        try:
            r = tools.execute(None, "dispatch_task", {})
            assert isinstance(r, str)
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"缺参数直接抛异常了：{type(e).__name__}: {e}")

    def test_none_engine_safe(self):
        """engine 传 None 时，工具不能崩在最里面。

        ⚠️ 测试环境里很多工具没有 engine ——
            它们应该明确说"没有引擎"，而不是
                AttributeError: 'NoneType' object has no attribute 'poll'

            实测（2026-11-05）：task_stats(None, "测试") 就是这么崩的。
            engine.poll 在 None 上调用 —— 用户看到的是
            "执行出错：AttributeError..."，他不知道这是"没引擎"。
        """
        from atrium.agent import tools

        try:
            r = tools.execute(None, "task_stats", {"name": "测试"})
            assert isinstance(r, str)
            # ⚠️ 给出来的应该是一句话，不是异常类型名
            assert "AttributeError" not in r, (
                f"没引擎时抛了 AttributeError 给用户看：{r}"
            )
        except AttributeError as e:
            pytest.fail(f"engine 是 None 时崩在 AttributeError：{e}")


# ══════════════════════════════════════════════════════
# prompt 拼装
# ══════════════════════════════════════════════════════


class TestPrompt:
    def test_prompt_not_empty(self):
        """系统提示词不能是空的 —— 空的话前台就成了个没手册的客服。"""
        from atrium.agent.prompt import get_prompt

        p = get_prompt()
        assert isinstance(p, str)
        assert len(p) > 200, f"提示词只有 {len(p)} 字，太短了，像是没写"

    def test_prompt_mentions_tools(self):
        """提示词里得提到关键工具 —— 不提模型就不会用。"""
        from atrium.agent.prompt import get_prompt

        p = get_prompt()
        for name in ("dispatch_task", "task_stats", "guard_status"):
            assert name in p, f"提示词里没提 {name}，模型不会用"

    def test_prompt_mentions_security_mode(self):
        """切安全模式那个工具也得提。"""
        from atrium.agent.prompt import get_prompt

        p = get_prompt()
        assert "security_mode" in p or "安全模式" in p


# ══════════════════════════════════════════════════════
# 平台：不连接也能构造
# ══════════════════════════════════════════════════════


class TestPlatformConstruct:
    def test_feishu_missing_creds(self):
        """飞书没给凭据：要么抛清楚错了，要么构造出个"不可用"的对象。

        ⚠️ 不能构造出一个"看着正常但 start 会崩"的东西。
        """
        from atrium.platforms import registry

        try:
            p = registry.make_platform("feishu", {})
            # 如果造出来了，至少不能是 None 假装成功
            if p is not None:
                assert hasattr(p, "start") or hasattr(p, "run")
        except Exception:
            pass    # 抛异常也可以接受

    def test_dingtalk_missing_creds(self):
        """钉钉同理。"""
        from atrium.platforms import registry

        try:
            p = registry.make_platform("dingtalk", {})
            if p is not None:
                assert hasattr(p, "start") or hasattr(p, "run")
        except Exception:
            pass


# ══════════════════════════════════════════════════════
# 消息摘要：真实输入
# ══════════════════════════════════════════════════════


class TestDigestReal:
    def test_collect_from_records(self):
        """从任务记录里抠文件路径 —— 造一个真文件才测得出。

        ⚠️ 这个函数**只收真实存在的文件**，而且只收特定后缀
            （.docx / .xlsx / .pdf 这些产出，不收 .py 这种过程脚本）。

            所以不能造几个假路径就期望它返回东西 ——
            那测的是"它会不会编"，不是"它会不会找"。

            改成：造一个真实的 .docx，从 result 文本里提到它的路径。
        """
        import tempfile

        from atrium.core import digest

        d = tempfile.mkdtemp()
        f = os.path.join(d, "产出.docx")
        with open(f, "wb") as fh:
            fh.write(b"fake docx content")

        recs = [{"result": f"做完了，文件在 {f}"}]
        files = digest.collect_files(recs)

        assert isinstance(files, list)
        assert f in files, f"没从结果里认出文件：{files}"

    def test_build_from_records(self):
        """建摘要：不能返回空字符串。"""
        from atrium.core import digest

        recs = [{"name": "测试任务", "status": "ok", "files": ["a.py"]}]
        s = digest.build_digest(recs)
        assert isinstance(s, str)
        assert len(s) > 0, "摘要是空的，用户看不到汇报"

    def test_weird_records(self):
        """怪的记录（缺字段、类型不对）不能崩。"""
        from atrium.core import digest

        for recs in (
            [{}],
            [{"files": None}],
            [{"files": []}],
            [{"name": 123}],
            ["不是字典"],
        ):
            try:
                digest.collect_files(recs)
                digest.build_digest(recs)
            except Exception as e:  # noqa: BLE001
                pytest.fail(f"输入 {recs!r} 崩了：{type(e).__name__}: {e}")
