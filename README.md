# atrium

**所有平台的门，都开到这儿。**

---

## 这是什么

用户可以在任何地方跟自己的电脑说话：

```
QQ      飞书     扣子     豆包     WorkBuddy
  │        │        │        │          │
  └────────┴────────┴────────┴──────────┘
                    │
              ┌─────▼─────┐
              │  atrium   │   ← 这个
              └─────┬─────┘
                    │
              ┌─────▼─────┐
              │  engine   │   真正干活的地方
              └───────────┘
```

**atrium 只做一件事：把消息从平台搬到引擎，再把结果搬回去。**

它不跑命令、不改文件、不做判断。那些是 engine 的事。

---

## 为什么要自己写

现在能用的东西都是一段一段拼起来的：

| 环节 | 现在用的 | 问题 |
|---|---|---|
| QQ | AstrBot + NapCat | 别人的，重，配置复杂 |
| 飞书 | AstrBot 插件 | 同上 |
| 扣子 | coze-bridge | 别人的，压缩代码，改不了 |
| 主动推送 | 无 | 扣子的桥不支持 |

**三个桥，三套配置，三种行为。**

而且都写着别人的名字 —— 出问题了不知道找谁，要加功能得求人。

atrium 把这些全收进来，一套配置、一个进程、一种行为。

---

## 支持什么

### 平台（收消息 / 发消息）

| 平台 | 协议 | 状态 |
|---|---|---|
| 飞书 | 开放平台 API（官方） | 第一个做 |
| 扣子 | Frontier WebSocket | 逆向过了，能做 |
| QQ | 官方机器人 API | 待定 |
| MCP 客户端 | MCP | 待定 |

### 对外协议（能被谁接）

| 协议 | 谁用 |
|---|---|
| MCP | Claude、Cursor、WorkBuddy、大多数 IDE |
| ACP | Zed、Obsidian、Neovim、JetBrains |
| OpenAI 兼容 | 任何能配 base_url 的客户端 |

---

## 设计原则

### 一、适配器和引擎分开

```
platforms/feishu.py      只管飞书收发的字节
platforms/coze.py        只管扣子的 WebSocket
platforms/qq.py          只管 QQ

engine                    只认一种内部消息格式
```

**加一个平台 = 加一个文件，不动别处。**

### 二、引擎是可替换的

atrium 不假设后面是什么：

```
现在的 remote-dispatch   一个 Python 进程
将来换个更好的          只改一个适配器
```

### 三、消息先落盘，再处理

```
收到消息
    ↓
写进 inbox（先落盘）
    ↓
确认收到（回平台"我收到了"）
    ↓
异步处理
    ↓
结果写进 outbox
    ↓
推送回原平台
```

**为什么：**平台会超时。如果同步等引擎跑完再回，用户看到的是"发送失败"。

**先确认，再干活** —— 这是所有靠谱 IM 系统的做法。

### 四、一个平台挂了不影响别的

```
飞书挂了     QQ 照跑
扣子断线     飞书照跑
    ↓
各自的重连、各自的队列
```

---

## 目录结构

```
atrium/
    core/
        message.py      内部消息格式（所有平台的公共语言）
        bus.py          收发总线
        queue.py        落盘的收发队列

    platforms/
        base.py         平台接口（所有平台实现它）
        feishu.py       飞书
        coze.py         扣子
        qq.py           QQ

    protocols/
        mcp.py          MCP Server
        acp.py          ACP Agent
        openai.py       OpenAI 兼容接口

    engine/
        client.py       调后面那个引擎的客户端

    cli.py              启动、状态、配置
```

---

## 内部消息格式

所有平台的消息，先转成这个：

```python
{
    "id": "本地唯一 id",
    "platform": "feishu",           # 从哪来
    "channel": "ou_xxx",            # 哪个会话（平台原样）
    "sender": "ou_xxx",             # 谁发的
    "text": "帮我看看那个文件",       # 说的话
    "attachments": [],              # 附件
    "reply_to": "",                 # 回哪条（平台原样）
    "ts": "2026-11-04T04:00:00+08:00",
    "raw": {},                      # 平台原始数据，排查用
}
```

**为什么保留 `raw`：**平台字段千奇百怪，丢了就再也拿不回来。
出问题的时候，`raw` 是唯一能复现现场的东西。

---

## 配置

一份文件管所有平台：

```json
{
  "engine": {
    "type": "remote-dispatch",
    "path": "D:/Project/dsh-bridge-oss"
  },
  "platforms": {
    "feishu": {
      "enabled": true,
      "app_id": "...",
      "app_secret": "..."
    },
    "coze": {
      "enabled": false,
      "pat": "sat_..."
    }
  }
}
```

**只开你要的。**没开的平台完全不加载。

---

## 跟 remote-dispatch 的关系

```
remote-dispatch    引擎（跑任务、带安全层）
atrium             门（收消息、发消息）
    ↓
atrium 可以单独用
remote-dispatch 也可以单独用（有自己的 HTTP 接口）
    ↓
**两个项目，松耦合**
```

不过 atrium 默认的引擎适配器就是 remote-dispatch。

---

## 状态

**刚开始。**现在只有这个 README 和骨架。

第一个目标：飞书收发跑通。

---

## 许可

MIT
