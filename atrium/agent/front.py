"""前台 Agent —— 在聊天窗口里跟你说话的那个。

## 它是什么

    不是转发器，是 Agent：

        收到一句话
            ↓
        模型**想**：这是闲聊？派活？问进度？
            ↓
        自己决定：直接答 / 调工具 / 两手都来
            ↓
        回一句

## 它能做什么

    工具（见 tools.py）：
        dispatch_task   派活
        task_stats      查进度
        task_list       列任务
        task_cancel     取消

    ⚠️ 它**没有**文件、没有终端。
       就算被注入，最多是"派错了个活"，
       而派出去的活会在引擎那边再过一遍关卡。

## 用什么模型

    从配置文件里读。跟引擎用的可以是同一套，也可以不同。
"""

from __future__ import annotations

import json
import sys
from typing import Callable

from atrium.agent import tools as ftools
from atrium.agent.prompt import get_prompt


SYSTEM = """你是 remote-dispatch 的前台。用户在聊天窗口里跟你说话。

你的职责是：听懂用户在说什么，然后

  · 闲聊、问问题、解释概念 —— 你自己答
  · 要改文件、跑命令、在电脑上干活 —— **派给电脑端**，你不自己干

⚠️ 你手上**没有**文件、也没有终端。你只有这四个工具：
    dispatch_task  派活
    task_stats     查进度
    task_list      列任务（用户说「继续那个」时先查）
    task_cancel    停任务

---- 派活手册 ----

"""


class FrontAgent:
    """前台 Agent。"""

    def __init__(self, llm, engine=None) -> None:
        """llm    engine.llm.LLM 那种（有 chat 方法）
        engine 引擎（派活用）
        """
        self.llm = llm
        self.engine = engine

    def reply(self, text: str, history: list[dict] | None = None,
              max_steps: int = 6) -> str:
        """给一句话，回一句。

        ⚠️ 这是一个独立的对话轮：
           不共享历史（除非传 history）。
           每一轮都是"这句话 + 手册 → 模型 → 回话"，干净、可预测。
        """
        task = (text or "").strip()
        if not task:
            return ""

        system = SYSTEM + get_prompt()

        messages: list[dict] = [{"role": "system", "content": system}]

        # 历史（可选）—— 只在同一次会话里用
        for h in (history or []):
            role = h.get("role")
            content = h.get("content")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": task})

        for _step in range(max_steps):
            try:
                reply = self.llm.chat(
                    messages=messages,
                    tools=ftools.TOOL_SPECS,
                    max_tokens=1500,
                )
            except Exception as e:  # noqa: BLE001
                return f"模型调用失败：{type(e).__name__}: {e}"

            # 没有工具调用 → 这就是最终回答
            if not reply.tool_calls:
                return (reply.content or "").strip() or "（没有输出）"

            # 有工具调用：记下它说的话，然后执行
            messages.append({
                "role": "assistant",
                "content": reply.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": tc.arguments,
                        },
                    }
                    for tc in reply.tool_calls
                ],
            })

            for tc in reply.tool_calls:
                try:
                    args = json.loads(tc.arguments or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}

                if self.engine is None:
                    out = "引擎没配好，派不了活。"
                else:
                    try:
                        out = ftools.execute(self.engine, tc.name, args)
                    except Exception as e:  # noqa: BLE001
                        out = f"工具出错：{type(e).__name__}: {e}"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": out,
                })

        return "（这一轮想太久了，先停一下。你可以说得更具体些。）"
