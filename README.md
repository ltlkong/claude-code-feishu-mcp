# claude-code-feishu-mcp

在飞书里直接和你本机的 Claude Code 对话。MCP Server 架构，流式卡片输出，手机上随时 code review、debug、问问题。

> 复用 Claude Max/Pro 订阅，不需要 API Key。不需要公网 IP。

---

## 为什么用这个

- **无需公网 IP** — 飞书 WebSocket 长连接，部署在家里的 Mac 上就行
- **流式卡片实时输出** — Claude 边想边输出，不是等半天发一坨文字
- **复用 Claude Max 订阅** — 作为 MCP Server 接入 Claude Code，不需要额外 API Key
- **语音消息** — 发语音自动转文字，还能语音回复（ElevenLabs TTS）
- **文件收发** — 图片/文档/音频/视频双向传输，Claude 直接分析
- **图片/视频内联** — `reply_image` 和 `reply_video` 在飞书内直接显示，不用下载
- **富文本消息** — `reply_post` 支持图文视频混排，一条消息搞定
- **定时提醒** — 通过 cron 创建定时消息和智能任务（UTC 自动转本地时区）
- **飞书卡片 V2** — 结构化回复用交互式卡片，带 emoji header 和颜色主题
- **智能 Heartbeat** — 用户不活跃后才触发（默认 10 分钟），活跃时不打扰
- **群聊选择性回复** — 群聊中智能判断是否需要回复，不相关的消息不出卡片
- **延迟卡片创建** — 卡片在 Claude 决定回复时才创建，失败自动恢复新卡片
- **用户画像** — 每用户每聊天独立 profile，自动注入消息上下文，支持动态更新
- **用户名解析** — 自动从群成员获取发送者真实名字
- **飞书云文档** — 创建飞书文档和多维表格（Bitable），支持多种字段和视图类型

## 架构

```
                          ┌─────────────────────────────────────────┐
                          │            Claude Code CLI              │
                          │                                         │
┌──────────┐  WebSocket   │  ┌─────────────────────────────────┐   │
│  飞书 App │◄────────────►│  │    feishu MCP Server            │   │
│  (用户)   │  长连接       │  │    (src/feishu_channel/)        │   │
└──────────┘              │  │                                 │   │
                          │  │  server.py    — MCP 入口 & 工具   │   │
                          │  │  feishu.py    — 飞书 API 客户端   │   │
                          │  │  card.py      — 卡片渲染引擎      │   │
                          │  │  media.py     — 音频/文件处理     │   │
                          │  │  reminder.py  — 定时提醒管理      │   │
                          │  │  heartbeat.py — 主动消息          │   │
                          │  │  config.py    — 配置管理          │   │
                          │  └─────────────────────────────────┘   │
                          │                                         │
                          │  ┌─────────────────────────────────┐   │
                          │  │  其他 MCP Servers (可选)          │   │
                          │  │  context7, chrome, gmail, etc.   │   │
                          │  └─────────────────────────────────┘   │
                          └─────────────────────────────────────────┘
```

飞书通过 WebSocket 推送消息到 MCP Server，Server 将消息注入 Claude Code 的对话流，Claude 通过 MCP 工具（`update_status`、`reply`、`reply_file` 等）实时回复飞书用户。

---

## 快速开始

### 1. 外部依赖

开始之前，确保以下工具已安装：

| 依赖 | 最低版本 | 安装方式 | 验证命令 | 说明 |
|------|---------|---------|---------|------|
| Python | 3.11+ | [python.org](https://www.python.org/) | `python3 --version` | 运行 MCP Server |
| Claude Code CLI | 最新 | `npm install -g @anthropic-ai/claude-code` | `claude --version` | 核心 AI 引擎 |
| Claude Max/Pro 订阅 | - | [claude.ai](https://claude.ai/) | `claude "hi"` 能正常回复 | CLI 需要有效订阅 |
| crontab | 系统自带 | macOS/Linux 内置 | `crontab -l` | 定时提醒功能依赖 |

### 2. 安装项目

```bash
git clone https://github.com/ltlkong/claude-code-feishu-mcp.git
cd claude-code-feishu-mcp
pip install -e .
```

### 3. 创建飞书应用

1. 打开 [飞书开放平台](https://open.feishu.cn/app)，点击「创建企业自建应用」
2. 填写应用名称（如 `Claude Code`），选择图标，点击创建

**添加机器人能力：**

进入应用详情 → 左侧菜单「添加应用能力」→ 添加「机器人」

**开启权限：**

进入「权限管理」，搜索并开启：

| 权限 scope | 说明 |
|-----------|------|
| `im:message` | 获取与发送单聊、群组消息 |
| `im:message:send_as_bot` | 以应用的身份发送消息 |
| `im:resource` | 获取消息中的资源文件（图片等） |
| `im:chat` | 获取群组信息 |
| `im:chat:readonly` | 获取群成员列表（用户名解析） |
| `contact:user.base:readonly` | 获取用户基本信息（可选，增强用户名解析） |

**启用长连接 & 事件订阅：**

左侧菜单「事件与回调」→「事件配置」→ 订阅方式选择「**使用长连接接收事件**」（不是 Webhook）→ 添加以下事件：

| 事件 | 说明 |
|------|------|
| `im.message.receive_v1` | 接收消息 |
| `card.action.trigger` | 卡片按钮点击回调 |

**添加卡片能力：**

左侧菜单「应用功能」→「卡片」→ 开启「卡片请求网址」使用长连接模式

**获取凭证：**

进入「凭证与基础信息」，复制 **App ID** 和 **App Secret**

**发布应用：**

「版本管理与发布」→ 创建版本 → 提交审核 → 管理员审核通过后可用

### 4. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入飞书凭证：

```bash
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

完整环境变量列表见下方[环境变量参考](#环境变量参考)。

### 5. 启动

在项目目录下运行：

```bash
claude --dangerously-load-development-channels server:feishu --dangerously-skip-permissions --chrome
```

| 参数 | 说明 |
|------|------|
| `--dangerously-load-development-channels server:feishu` | 加载飞书 MCP Server 作为消息通道 |
| `--dangerously-skip-permissions` | 跳过工具调用确认（无人值守运行必需） |
| `--chrome` | 启用 Chrome 浏览器控制能力（网页操作、截图等） |

启动后飞书消息即刻可达。

---

## 使用方式

### 基本对话

在飞书中给机器人发消息，Claude 会实时以卡片形式回复。支持私聊和群聊。

### 发送文件

直接在飞书里发图片、文档、音频，Claude 会自动下载并分析。生成的文件会通过飞书发回给你。

### 语音消息

发语音给机器人，自动转文字后处理。需要在 `.env` 配置 ElevenLabs 才能语音回复。

### 定时提醒

在对话中告诉 Claude 你想设置什么提醒，它会通过 `create_reminder` 工具自动创建 cron 任务。

**两种模式：**
- **简单提醒** — 到时间发送固定消息
- **智能任务** — 到时间触发 Claude 思考后决定回复内容

示例：「每天早上 9 点提醒我看日报」「每周五下午 3 点总结本周做了什么」

### Heartbeat（主动消息）

Heartbeat 定期回顾当前对话历史，如果有值得主动说的事（跟进、提醒、洞察），会自动发消息。没事就不发。

- chat_id 从最近的飞书消息自动获取，不需要手动配置
- 模型和间隔可在 `.env` 配置

---

## 环境变量参考

| 变量 | 必填 | 默认值 | 说明 |
|------|:---:|-------|------|
| `FEISHU_APP_ID` | 是 | - | 飞书应用 App ID |
| `FEISHU_APP_SECRET` | 是 | - | 飞书应用 App Secret |
| `ALLOWED_USER_IDS` | 否 | 空（允许所有） | 允许使用的飞书用户 ID 列表，逗号分隔 |
| `ELEVENLABS_API_KEY` | 否 | - | ElevenLabs TTS API Key（语音回复） |
| `ELEVENLABS_VOICE_ID` | 否 | - | ElevenLabs 语音 ID |
| `TEMP_DIR` | 否 | `/tmp/feishu-channel` | 临时文件目录 |
| `TEMP_FILE_MAX_AGE_HOURS` | 否 | `2` | 临时文件保留时长（小时） |
| `STALE_CARD_TIMEOUT_MINUTES` | 否 | `30` | 卡片超时清理时间（分钟） |
| `HEARTBEAT_MODEL` | 否 | `haiku` | Heartbeat 使用的 Claude 模型 |
| `HEARTBEAT_INTERVAL_MINUTES` | 否 | `10` | Heartbeat 轮询间隔（分钟） |
| `HEARTBEAT_INACTIVITY_MINUTES` | 否 | `10` | 用户不活跃多久后触发 Heartbeat |

---

## MCP 工具

| 工具 | 说明 |
|------|------|
| `update_status(request_id, status, text, emoji, template)` | 更新飞书卡片内容（带 emoji 和颜色主题） |
| `reply(request_id, text)` | 发送最终回复并关闭卡片 |
| `reply_file(chat_id, file_path)` | 发送文件到飞书群聊 |
| `reply_image(chat_id, image_path)` | 发送图片（内联显示，非文件下载） |
| `reply_video(chat_id, video_path)` | 发送视频（内联播放，自动生成封面） |
| `reply_post(chat_id, content, title)` | 发送富文本（图文视频混排） |
| `reply_audio(chat_id, text)` | 文字转语音发送（需配置 ElevenLabs） |
| `create_reminder(id, cron, chat_id, message, smart, max_runs)` | 创建定时提醒（UTC 自动转本地时区） |
| `list_reminders()` | 列出所有提醒 |
| `delete_reminder(id)` | 删除提醒 |
| `create_doc(title, content, chat_id)` | 创建飞书云文档 |
| `create_bitable(title, fields, records, views, chat_id)` | 创建飞书多维表格 |
| `update_profile(chat_id, user_id, profile)` | 更新用户画像（per chat per user） |
| `read_messages(chat_id, count)` | 读取聊天历史消息 |
| `send_reaction(message_id, emoji)` | 给消息发送表情回应 |
| `bitable_records(action, app_token, table_id, ...)` | 多维表格记录增删改查 |
| `manage_task(action, summary, ...)` | 飞书任务创建/列表/更新/完成 |
| `search_docs(query)` | 搜索飞书云文档（docs/sheets/bitables） |

---

## 项目结构

```
claude-code-feishu-mcp/
├── src/feishu_channel/       # MCP Server 核心
│   ├── server.py             # MCP 入口，工具定义，消息路由
│   ├── feishu.py             # 飞书 API 客户端（WebSocket、消息、卡片）
│   ├── card.py               # 飞书卡片 V2 渲染引擎
│   ├── media.py              # 图片/音频/文件下载，TTS，语音转写
│   ├── reminder.py           # Cron 提醒管理（简单消息 + 智能任务）
│   ├── heartbeat.py          # Heartbeat 主动消息（自动回顾对话历史）
│   └── config.py             # Pydantic Settings 配置
├── profiles/                    # 用户画像（per chat per user，gitignore）
├── workspace/
│   └── skills/               # Claude Skills（feishu-card 等）
├── CLAUDE.md                 # Claude 人设和行为规则
├── HEARTBEAT.md              # Heartbeat prompt 模板
├── .mcp.json                 # MCP Server 注册
├── pyproject.toml            # Python 项目配置
├── .env.example              # 环境变量模板
└── .env                      # 环境变量（不提交）
```

---

## English

**claude-code-feishu-mcp** is an MCP Server that bridges Feishu/Lark messenger with your local Claude Code CLI via WebSocket.

Key features:
- No public IP needed (Feishu WebSocket long-polling)
- Streaming card output (real-time typing effect via Feishu Card V2 with emoji headers)
- Runs as an MCP Server — native Claude Code integration
- Reuses Claude Max/Pro subscription (no API key required)
- Voice messages (Whisper STT + ElevenLabs TTS)
- Inline image/video display + rich text posts (mixed text/image/video)
- File upload/download between Feishu and local machine
- Cron-based reminders with UTC auto-conversion to local timezone
- Smart heartbeat — only triggers after user inactivity
- Selective group chat replies — AI decides whether to respond in group chats
- Deferred card creation — cards appear only when AI decides to respond, auto-recovery on failure
- Per-user per-chat profiles — auto-injected into message context for personalized responses
- User name resolution from chat members
- Feishu Docs & Bitable — create cloud documents and multi-dimensional spreadsheets
- Works alongside other MCP servers (browser, gmail, notion, etc.)

### External dependencies

| Dependency | Install | Required |
|-----------|---------|:--------:|
| Python 3.11+ | [python.org](https://www.python.org/) | Yes |
| Claude Code CLI | `npm install -g @anthropic-ai/claude-code` | Yes |
| Claude Max/Pro subscription | [claude.ai](https://claude.ai/) | Yes |
| crontab | Built-in on macOS/Linux | Yes (for reminders) |

### Quick start

```bash
git clone https://github.com/ltlkong/claude-code-feishu-mcp.git
cd claude-code-feishu-mcp
pip install -e .
cp .env.example .env
# Edit .env with your Feishu app credentials
claude --dangerously-load-development-channels server:feishu --dangerously-skip-permissions --chrome
```

See the Chinese sections above for detailed Feishu app setup instructions.

---

## License

[MIT](LICENSE)
