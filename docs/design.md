# 重做：手机上 vibecoding 的 Claude Code / Codex 客户端

写于 2026-11-11。取代 [agent-first.md](agent-first.md) 的"改造 Heeler"路线。

## 为什么重做

试用 Heeler 改造版后的结论：还是太复杂，输入还是麻烦。根子不在界面，而在接 agent 的方式：
Heeler 通过 herdr 往 agent 的**终端界面里打字**，再**读屏幕**判断有没有送到、有没有弹窗。
于是有了"输入框不空不能发""未送达""Blocked 弹窗解析"、工具键盘这些补丁。
只要还隔着终端界面，就做不出 Claude / Codex 官方 App 那种顺手的对话。

新路线：直接用两个 CLI 自带的结构化协议，**没有终端这一层**。

| | Claude Code（本机 2.1.322） | Codex（本机 0.167.0） |
| --- | --- | --- |
| 协议 | `claude -p --input-format stream-json --output-format stream-json --include-partial-messages` | `codex app-server`（JSON-RPC） |
| 逐字流式 | partial messages | `item/agentMessage/delta` |
| 授权 / 提问 | `--permission-prompts host`：由我们回答 | `item/*/requestApproval`、`item/tool/requestUserInput` |
| 运行中再发消息 | 流式输入会排队 | `turn/steer` |
| 停止 | control `interrupt` | `turn/interrupt` |
| 续接电脑上的会话 | `--resume <id>` | `thread/list`、`thread/resume` |
| 断线保活 | 没有，需要主机上的小助手 | 自带：`codex app-server daemon bootstrap`（官方说明就是给 SSH 驱动用的） |
| 其他 | | `fuzzyFileSearch`（`@文件`）、`turn/diff/updated`（每轮改动）、`thread/name/set`、`model/list` |

以上都在 2026-11-11 用真实小请求验证过（见下面“实现记录”）。

## 架构

```
iPhone App ──SSH──▶ 主机
                     ├─ 小助手（常驻，Unix socket）
                     │    ├─ claude -p … stream-json   （每个 Claude 会话一个进程）
                     │    └─ codex app-server          （Codex 自带常驻 daemon）
                     └─ 会话记录：~/.claude/projects、~/.codex/sessions（电脑上的历史）
```

- **只用 SSH**：一条 SSH 连接，exec 一个 `attach` 命令接到小助手上，所有会话复用这一条。没有中继。
- **主机小助手**（`helper/steerd.py`）：Python 3.8+ 单文件，App 自带，连上时自动装到
  `~/.local/share/steer/`，版本不一致就自动升级。它让手机断开后 agent 继续跑，把两边的协议
  翻译成同一种“时间线”（消息、工具步骤、计划、提示、一轮结束），手机连上或回来时给一份快照，
  之后推增量。历史不另存：Claude 的记录读它自己的 `~/.claude/projects/*.jsonl`，Codex 用
  `thread/read`。协议细节见 [helper-protocol.md](helper-protocol.md)。
- **Codex**：小助手自己起一个 `codex app-server` 子进程长期持有，所以手机断开时 Codex 也感觉不到，
  不需要 Codex 的 daemon。
- **App**：全新 SwiftUI 工程。SSH 底层直接用 Heeler 的 `HeelerSSH` 包（libssh2 + OpenSSL，
  Apache-2.0，见 NOTICE），其余重写。
- **herdr**：新 App 不依赖它。手机上开的 agent 跑在主机后台，不出现在 herdr 窗口里；
  电脑上用过的 Claude / Codex 会话可以在手机上"接着聊"。

## 用户体验

### 手机 vibecoding 的难点，和对应的设计

| 难点 | 设计 |
| --- | --- |
| 打字慢 | 输入框大、用系统键盘和自己的输入法（语音听写自然可用）；快捷短语一点就发；`@` 搜文件不用敲路径；`/` 选命令；历史提示词可复用 |
| 屏幕小 | 只突出对话和要你做决定的东西；工具调用折叠成一行摘要 |
| 断断续续地看 | 首页"需要你"置顶；每个会话显示离开期间发生了什么；未读标记 |
| 网络不稳、App 被挂起 | agent 在主机上继续跑；回到 App 自动补齐；发出的消息不丢，断线时排队 |
| 授权打断节奏 | 授权卡片停在输入框上方（拇指区），一点允许；"本会话都允许"；一键切到自动改文件模式 |
| 手机上不好看改动 | 每一轮显示改了哪些文件 +/-，点开看 diff |
| 开始一个任务太繁琐 | 点 ＋ → 直接打字 → 发送就是创建；项目默认上次用的 |

### 首页：会话列表

```
┌─────────────────────────────┐
│ 会话                 Mac ▾  ＋ │
│ [全部] [herdr-mobile] [islander]│
│ 需要你                         │
│ ✳ 修复登录重试      ⚠ 等你批准   │
│   herdr-mobile · 要运行 npm test │
│ 进行中                         │
│ ◎ 重构 API 客户端   ● 运行中 2:13│
│   islander · 正在编辑 api.ts    │
│ ✳ 加深色模式        ✓ 3 分钟前 • │
│   herdr-mobile · 改了 3 个文件   │
└─────────────────────────────┘
```

- 按时间排，"需要你"（授权、提问、计划待批）永远在最上面；运行中显示计时。
- 顶部项目标签用来筛选，不分组嵌套。每行：agent 图标、标题、项目、状态、最后一句话。
- 左滑：归档 / 停止。下拉：刷新并补齐。
- 列表里也能看到电脑上最近的 Claude / Codex 会话（标"电脑上"），点进去可以接着聊。

### 新建：打字就是开始

点 ＋ 直接进入空对话，键盘已经弹起。顶部两个小标签 `[herdr-mobile ▾] [Claude ▾]`，默认上次用的，
点开选项目（最近用过的在前，可搜索、可浏览目录）或换 Codex。打完字点发送，会话就建好了。

### 对话页（核心）

```
┌─────────────────────────────┐
│ ‹ 修复登录重试             ⋯  │
│   herdr-mobile · main         │
│              把重试改成保留购物车 │
│ 好的，我先看一下支付流程。       │
│ ▸ 1 分 20 秒 · 读 3 个文件 · 改 2 个 +24 −6 │
│ 改好了，重试会保留购物车……       │
│ ┌ 要运行命令 ───────────────┐ │
│ │ npm test                  │ │
│ │ [         允许          ] │ │
│ │ [本会话都允许]    [拒绝…]  │ │
│ └───────────────────────────┘ │
│ [继续] [跑一下测试] [提交] [解释] │
│ ┌───────────────────────────┐ │
│ │ 给 Claude 发消息…           │ │
│ │ ＋   询问 ▾   Opus ▾     ⬆ │ │
│ └───────────────────────────┘ │
│           系统键盘              │
└─────────────────────────────┘
```

- **消息**：我的消息靠右气泡；agent 的回复逐字流出，Markdown 渲染，代码块可复制。
- **工具调用**：每一轮的工具步骤折叠成一行"用时 · 读了几个文件 · 改了几个 +/-"，点开看每一步；
  改文件的步骤点开是 diff；命令输出默认只看最后几行。
- **运行中**：顶部一条细状态"正在运行 npm test · 0:42"；输入框为空时发送键变成停止键。
  运行中发的消息不用等：Codex 直接插话，Claude 排在下一步，气泡上标"排队中"。
- **需要你的时候**：授权、提问、计划审批都是卡片，停在输入框正上方。
  - 授权：整行宽的"允许"，下面"本会话都允许""拒绝…"（拒绝时可以顺便写一句让它换个做法）。
  - 提问：选项是大按钮，"其他"就用输入框回答。
  - 计划：完整计划可滚动阅读，"开始执行"或在输入框里提修改意见。
- **输入框**：系统键盘，自己的输入法和语音听写都能用；回车换行，发送靠按钮（可在设置里改）。
  - 多行自动长高到屏幕约四成，再多可以展开成全屏编辑。
  - `＋`：照片和截图（做界面时最有用）、拍照、文件、粘贴。
  - `@` 弹出项目文件搜索；`/` 弹出命令和技能。
  - 下面的小标签切换模式（Claude：询问 / 自动改文件 / 计划 / 全自动；Codex：询问 / 自动 / 完全访问）
    和模型，下一轮生效。
  - 输入框上方一排快捷短语（继续、跑一下测试、提交、解释一下改动……），一点就发，可以自定义。
  - 每个会话的草稿自动保存。
- **右上角菜单**：改名、停止、查看本会话所有改动、复制"在电脑上继续"的命令（`claude --resume …`）。

### 断线与后台

- 打开 App 或回到前台自动连上，从断点补齐，不打扰；连不上时顶部一条提示，消息先排队。
- App 关掉后 agent 照常在主机上跑。没有中继，第一版**收不到推送**；之后可以让主机小助手
  通过 Bark / ntfy 这类服务推送"做完了 / 等你批准"。

### 首次设置

添加主机：地址、端口、用户名，密码或设备密钥（显示一行 `authorized_keys` 让你复制）。
连上后自动检查：主机上有没有 `claude` / `codex`、有没有 `python3`，然后一键装小助手。
三步完成，之后不再出现。

## 第一版范围

做：一台或多台 SSH 主机；Claude Code 和 Codex；会话列表、新建、流式对话、授权 / 提问 / 计划卡片、
停止、运行中插话、模式和模型切换、图片附件、`@文件`、快捷短语、续接电脑上的会话、断线补齐、每轮改动和 diff。

先不做：终端、中继和推送、iPad 专门布局、Live Activity、其他 agent（Gemini、OpenCode……）、
完整的 Git 操作界面。

## 实施顺序

1. **协议验证**：在本机用小请求跑通 Claude stream-json（流式、授权、提问、计划、中断、续接）和
   Codex app-server（同上），录下真实消息做测试样本。
2. **主机小助手**：保活、事件编号落盘、补发；本机跑通。
3. **App 骨架**：SSH 连接 → 小助手 → 一个会话能收发、流式显示。
4. **对话体验**：卡片、输入框、快捷短语、附件、`@文件`、模式模型。
5. **首页和新建**、续接电脑会话、首次设置流程。
6. 真机：自己的 Team 和 Bundle ID。

## 实现记录（2026-11-11）

协议验证（claude 2.1.322、codex-cli 0.167.0，Haiku / gpt-5.6-luna 小请求）：

- Claude：`claude -p --input-format stream-json --output-format stream-json --verbose
  --include-partial-messages --permission-prompt-tool stdio`。先发 `control_request initialize`
  （返回命令列表和模型列表）。授权、提问（`AskUserQuestion`）、计划（`ExitPlanMode`）都以
  `control_request can_use_tool` 到达，回 `control_response`：提问的答案放在
  `updatedInput.answers`，键是问题原文；“本会话都允许”把 `permission_suggestions` 的
  `destination` 改成 `session` 放进 `updatedPermissions`；不带说明的拒绝加 `interrupt: true`。
  运行中再发的消息会排队，作为下一轮处理。`--session-id` 预先指定会话 id，`--resume` 续接后 id 不变。
  编辑结果带 `structuredPatch`，可以直接画 diff。
- Codex：`codex app-server` JSON-RPC。授权请求先于对应的 `item/started` 或同时到达；
  `clientUserMessageId` 会回到 `userMessage.clientId`，用来把手机上的气泡和 Codex 的回显合并；
  `turn/steer` 可以在运行中插话；`serverRequest/resolved` 表示请求已了结。
- **macOS 钥匙串**：从 SSH 会话里直接启动的 Claude 读不到钥匙串里的登录（报 “Not logged in”）。
  小助手因此在 macOS 上以 LaunchAgent（`gui/<uid>` 域，`com.kadaliao.steer.helper`）运行，
  这样就在已登录的桌面会话里。电脑没登录桌面时会退回普通后台进程，App 设置页会提示。
- 远程登录 shell 可能是 fish，所以 App 发出的命令一律包在 `/bin/sh -c '…'` 里。
- 端到端：Simulator 里的 App 通过 SSH 连本机 127.0.0.1:22，新建 Claude 和 Codex 会话、
  批准授权、快捷短语追问、查看 diff 都跑通（`UITests/ConversationUITests.swift`）。
