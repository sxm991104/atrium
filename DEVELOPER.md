# atrium 开发者手册

> 想让 atrium 跑起来的，看 [README](README.md)。
> 这一页是给**要改它的人**看的。

---

## 为什么自己写

现在能用的东西都是一段一段拼起来的：

| 环节 | 原来用的 | 问题 |
|---|---|---|
| 飞书 | AstrBot 插件 | 别人的，重，配置复杂 |
| 钉钉 | 无 | 要自己实现 |
| 主动推送 | 无 | 各家的桥都不给 |

而且都写着别人的名字 —— 出问题不知道找谁，要加功能得求人。

atrium 把这些收进来，一套配置、一个进程、一种行为。

---

---

## 两个关键的设计决定

### 一、异步，不是同步

```
统一的同步做法（ACP / 一问一答）：

    用户说 → 引擎跑（可能两分钟）→ 才回
        ↓
    平台早就超时了，用户看到"发送失败"
        ↓
    而且跑一个任务就占住一个会话，多任务没法并发

atrium 的做法：

    用户说 → **立刻回"派下去了"** → 引擎后台跑
                ↓
          跑完 → **主动推结果回来**
```

**这就是为什么不做 ACP** —— 它是同步的，跟我们要的正好相反。

### 二、先落盘，再处理

```
收到消息 → 写盘 → 回"收到" → 后台慢慢处理
    ↓
平台 3 秒超时不关心了，因为我们已经应答
    ↓
进程崩了也不丢消息，重启接着处理
```

---

---

## 支持什么

### 平台

| 平台 | 状态 | 收消息 | 发消息 | 发文件 |
|---|---|---|---|---|
| **钉钉** | ✅ | Stream 长连接 | sessionWebhook | OAPI 上传 |
| **飞书** | ✅ | lark-oapi 长连接 | HTTP API | 待做 |

**只接纯聊天平台。**

AI 平台（扣子、WorkBuddy、豆包）自带桥和 Agent，不走这儿。
它们自己能主动发消息，我们没必要在中间再插一层 ——
插进去还得多花一份模型钱。

### 加一个平台

```python
# atrium/platforms/dingtalk.py

@register("dingtalk")
def _make(cfg: dict) -> Platform:
    return DingTalk(cfg["client_id"], cfg["client_secret"])


class DingTalk(Platform):
    name = "dingtalk"

    def start(self, on_message):        # 连上，阻塞收消息
    def send(self, reply) -> bool:      # 发文字
    def send_file(self, reply) -> bool: # 发文件（可选）
```

然后在 `atrium/platforms/__init__.py` 里导入一行。

**主程序不用改。**

---

---

## 目录结构

```
atrium/
    cli.py              命令行：init / run / status / test

    core/
        message.py      内部消息格式（所有平台的公共语言）
        queue.py        落盘队列（inbox / outbox / done / failed）
        digest.py       批量汇总：一个任务用列表，多个用表格
        watch.py        进度监工：盯任务，跑完一批主动报
        app.py          主程序：串平台、队列、引擎、前台

    agent/
        front.py        前台 Agent（接模型，会调工具）
        prompt.py       派活手册（哪些话能派、任务名怎么起）
        tools.py        工具声明 + 分发（13 个，见下）
        guard_tools.py  验证码 / 提权 / 解锁那五个
        extra_tools.py  续话 / 查模型 / 查状态 / 查卡住的问题
        memory.py       对话记忆（记得刚才派了什么）

    engine/
        client.py       引擎适配器（走命令行：派活、查进度）
        api.py          引擎 HTTP 客户端（走 8790：验证码、权限、状态）

    platforms/
        base.py         平台接口
        registry.py     平台注册表
        feishu.py       飞书
        dingtalk.py     钉钉
        dingtalk_file.py  钉钉发文件
```

---

---

## 内部消息格式

所有平台的消息，先转成一个样子：

```python
{
    "id": "本地唯一 id",
    "platform": "dingtalk",
    "channel": "cidxxx",            # 哪个会话
    "sender": "user_staff_id",      # 谁发的
    "text": "帮我看看那个文件",
    "attachments": [],
    "reply_to": "msgxxx",
    "ts": "2026-11-04T06:00:00+0800",
    "meta": {"session_webhook": "..."},   # 回消息 / 发文件要用
    "raw": {},                      # 平台原始数据，排查用
}
```

**`raw` 和 `meta` 一定要留。**

平台字段千奇百怪而且会变。出问题时这两个是唯一能复现现场的东西。

---

---

## 配置

`~/.atrium/config.json`：

```json
{
  "engine": {
    "type": "remote-dispatch",
    "path": "D:/Project/dsh-bridge-oss"
  },
  "platforms": {
    "feishu": {
      "enabled": true,
      "app_id": "cli_xxx",
      "app_secret": "xxx"
    },
    "dingtalk": {
      "enabled": true,
      "client_id": "dingxxx",
      "client_secret": "xxx"
    }
  }
}
```

**只开你要的。**没开的平台完全不加载。

### 用哪套模型

前台 Agent 要模型（判断这是闲聊还是派活）。

配置文件里没有 `front` 段时，**自动借引擎那套**：

```
%APPDATA%/dsh-bridge/engine.json 里的 active profile
```

要自己指定就加：

```json
{
  "front": {
    "base_url": "https://api.deepseek.com",
    "api_key": "sk-xxx",
    "model": "deepseek-flash"
  }
}
```

---

---

## 前台能干哪些事

**前台是个 Agent，靠工具干活。一共 13 个：**

| 工具 | 干什么 | 走哪条路 |
|---|---|---|
| `dispatch_task` | 派活 | 命令行 |
| `task_stats` | 查一个任务的进度 | 命令行 |
| `task_list` | 列所有任务窗口 | 命令行 |
| `task_cancel` | 停一个任务 | 命令行 |
| `confirm_dangerous_action` | 危险操作三级确认（第 3 级发码） | HTTP |
| `verify_task_code` | 校验用户报的验证码（针对某一件活） | HTTP |
| `unlock_all` | 解锁整个任务端 | HTTP |
| `task_permission` | 查 / 改某个窗口的权限档 | HTTP |
| `guard_status` | 验证码那块配好没有 | HTTP |
| `continue_task` | 往已有窗口续话（**「继续」必须用它**） | HTTP |
| `list_models` | 有哪些模型可用（**别让模型瞎猜名字**） | HTTP |
| `check_asks` | 有哪些任务卡在等用户回答 | HTTP |
| `engine_status` | 任务端整体状态（锁没锁） | HTTP |

### 为什么分两条路

```
命令行（core/bridge.py dispatch / stats / list / cancel）
    · 一次性动作，不依赖那个服务在跑
    · 引擎挂了它也能派活（因为它就是起进程）

HTTP（127.0.0.1:8790）
    · 验证码 / 权限 / 状态这些**有状态**的东西
    · 状态在引擎进程里，命令行没暴露
    · 需要桌面端起起来（它才是那个服务）
```

**⚠️ 服务没起来时，那 9 个 HTTP 工具会明确说"桌面端没起"，**
**而不是报一堆 urllib 的错。**

### 「继续」为什么必须用 continue_task

```
用户说「继续」，如果重派一个新任务：
    · 会被当成又开一件新活
    · 可能又触发一遍成本检查、又弹一遍同一个问题
    · 来回打转 —— 实测卡死过整条链路

continue_task 做的是：把这句话送进**原来那个窗口**，让上下文接着走。
```

### 验证码有两种，别搞混

```
verify_task_code   验的是「某一件具体的活」
                   用户说「那个登录页的码我收到了」

unlock_all         解的是「整个任务端锁死」
                   用户说「现在什么都做不了」

拿错会验不过 —— 那不是码错，是类型错。
```

---

---

## 推送长什么样

任务跑完不一条条刷屏，攒一批合成一条。

**一个任务（列表）：**

```
✅ 跑完了：写文档DS
（模型 DeepSeek，29.4 秒）

文档已生成并核对通过。

产出：测试文档.docx（34KB）
```

**多个任务（表格，按模型分组）：**

```
**5 个任务跑完了**：成功 4，失败 1

**DeepSeek**（3 个）

| 任务 | 状态 | 耗时 | 产出 |
|---|---|---|---|
| 写文档DS | ✅ | 29s | 测试文档.docx |
| 查时间DS | ✅ | 7s | — |
| 算个数DS | ✅ | 2s | — |

**Kimi**（2 个）

| 任务 | 状态 | 耗时 | 产出 |
|---|---|---|---|
| 算个数KM | ❌ | 30s | 超时 |
| 查磁盘KM | ✅ | 18s | — |
```

然后**文件单独发过去**，手机上能直接下载。

### 为什么要分档

```
1 个      → 列表（用户想看细节）
2-20 个   → 表格（用户想横向对比）
20+ 个    → 只报统计和失败的（再多就是折磨）
```

### 为什么要按模型分组

同时开 DeepSeek 和 Kimi 的时候，一批推送里混着两边的任务。

不分组的话用户看不出哪个是哪个 —— 而这恰恰最要紧：
哪个快、哪个慢、哪套老出错。

### 为什么要等一会儿

```
第一个跑完就报 → 10 个任务会推 10 次
    ↓
第一个跑完后再等 8 秒，这期间的全算一批
    ↓
一次报完
```

---

---

## 只发"这次产出的"文件

工作目录里会攒历史文件。

**按任务开始时间过滤 mtime**：只有任务开始之后改过的文件才算这次的产出。

不然用户派一次活，会收到上周三个旧文档。

---

---

## 跟 remote-dispatch 的关系

```
remote-dispatch    引擎（跑任务、带安全层：守卫 / 影子执行 / 审计 / 回退）
atrium             门（收消息、前台 Agent、发消息）
    ↓
两个项目松耦合
atrium 默认的引擎适配器就是 remote-dispatch
```

---

---
