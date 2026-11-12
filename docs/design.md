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
点开选项目（最近用过的在前，可搜索、可浏览目录，也能当场新建文件夹）或换 Codex。打完字点发送，会话就建好了。

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
│ ✨ 跑一下测试                 ⬆ ✎ │
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
  - `@` 弹出项目文件搜索；`/` 弹出这个项目里的命令和技能（Codex 用 `$` 选技能）。
    列表右上角可以关掉。键盘可以用输入框里的按钮收起，也可以点或下拉对话区收起。
  - 下面的小标签切换模式（Claude：询问 / 自动改文件 / 计划 / 全自动；Codex：询问 / 自动 / 完全访问）
    和模型，下一轮生效。
  - 输入框为空时，上方显示 Claude 自己猜的下一句（CLI 的 prompt suggestion），一点就发，
    铅笔按钮先填进输入框改一改。Codex 没有这个功能，就不显示。
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

做：一台或多台 SSH 主机（同时连着，首页看全部或只看一台）；Claude Code 和 Codex；会话列表、新建、流式对话、授权 / 提问 / 计划卡片、
停止、运行中插话、模式和模型切换、图片附件、`@文件`、`/` 命令和技能、建议的下一句、续接电脑上的会话、断线补齐、每轮改动和 diff。

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

## 第二轮（2026-11-11，真机试用后的反馈）

- **多台主机**：每台主机一个 `HostConnection`，同时保持连接；首页默认合在一起按时间排，
  标题处切换"全部主机 / 某一台"，新建会话可以选主机。
- **收起键盘**：输入框里有收起键盘的按钮；点或下拉对话区也会收起。
- **建议的下一句**：预置快捷短语不实用，删掉了。Claude 用 `--prompt-suggestions`，
  每轮结束约 3 秒后发 `{"type":"prompt_suggestion","suggestion":"…"}`（有时不发）；
  小助手放进会话摘要的 `suggestion`，App 显示成一行，点一下就发。
- **命令和技能**：之前只有在小助手里跑过一次 Claude 会话后才有列表，新主机上 `/` 是空的，
  Codex 一直是空的。现在按项目取：Claude 起一个只回答 `initialize` 的短命进程
  （关掉 hooks、不发消息，不花用量、不留记录，约 0.3 秒），Codex 用 `skills/list`；
  Codex 消息里的 `$技能名` 会附上 `{"type":"skill"}` 输入，和 Codex TUI 一样。
- **@ / 列表能关**：列表标题右边有关闭按钮，关掉后直到下一个 `@` `/` `$` 才再出现。
- **电脑终端里开着的会话**：手机上接着聊时，小助手另起 `claude --resume` 进程，新内容写进
  同一个记录文件；终端里的进程不会重新读，要退出再 resume 才看得到，在旧窗口接着打字会分叉。
  小助手现在读 Claude Code 的会话登记（`~/.claude/sessions/<pid>.json`）准确判断
  "终端正开着"，App 里明确提示。

## 第三轮（2026-11-11）：终端和手机同步

查过官方文档和本机 CLI 后的结论：

- **Codex 能真同步。** 新版 Codex 在电脑上有一个共享的后台服务（app-server daemon），终端里的
  `codex` 默认连它。小助手改成也连这个服务（它的控制 socket 讲 WebSocket，小助手用标准库写了个
  最小的 WebSocket 客户端），终端和手机就是同一个进程里的同一个会话：两边实时看到对方的每一轮，
  审批请求两边都收到，谁先点算谁的。
- **Claude 没有公开的办法。** 文档里没有让第二个客户端接入正在运行的会话的协议；官方的
  Remote Control 能做到终端、网页和官方 App 同步，但只给官方客户端用。所以 Steer 做的是
  “同一时间只有一个进程，交接不分叉”：
  - 终端开着时，手机实时跟着看（读 Claude 的记录文件和会话登记里的状态）。
  - 手机发消息时先等终端空闲，然后在 herdr / tmux 窗格里按两次 Ctrl+C 关掉它（普通 shell 发
    SIGTERM），再由小助手接着跑。
  - 手机这边做完、空闲一分钟后，自动在原窗格里用原来的参数加 `--resume` 重新打开。
  - 判断窗格时要小心嵌套：在 herdr 里起的 tmux，里面的进程两套环境变量都有。现在按“哪个窗格的
    shell 是这个进程最近的祖先”来定，确认不了就当普通 shell 处理，绝不往别的窗格里按键。
- 有任务在跑或在等你回答时，小助手用 `caffeinate` 防止电脑睡眠。
- 新版助手装上时如果旧的正忙，App 每 30 秒问一次，旧的空下来就换新的；只有小助手自己手里的活
  （它起的 Claude 进程、待回答的请求、交接中）才会让它拒绝退出。

## 第四轮（2026-11-11）：推送

- 手机上发起的那一轮做完、需要批准或回答、出错时推送；终端里你自己跑的不推送。
- 不经过任何中继：主机助手用团队的 APNs 密钥（`N7G3K4DTMB`，Sandbox & Production，团队范围）
  自己签 JWT（系统 openssl），用系统 curl 的 HTTP/2 直接发给 APNs。密钥放在每台电脑的
  `~/.local/share/steer/apns/`，备份在 `~/.private_keys/`。
- App 把设备 token 连同"这台主机在 App 里的 id"登记给每台电脑的助手，点通知直接进那个会话；
  正在看的那个会话不弹横幅。Xcode 装的调试版走沙盒，TestFlight / App Store 版走正式环境。
- 模拟器（Apple 芯片）能收真推送，端到端测试 `testPushNotifications` 用的就是它；模拟器的
  apsd 偶尔连不上推送服务器（日志里 “Connected on 0 interfaces”），重启模拟器就好。

## 第五轮（2026-11-12）：真机试用 /clear 时暴露的问题

- **/clear 和 /new 改成"新会话"。** 手机上发 /clear 原先会照常发给 Claude：开在 herdr 里的会话先被
  接管关掉，Claude 在小助手的进程里清空后其实换了一个新会话 ID，交还时却用旧 ID resume，旧内容又
  回来了，手机界面也没清。现在 App 自己处理：在同一台电脑、同一个项目里新建一个空会话并跳过去，
  终端里开着的旧会话不动。
- **收起空闲进程和发消息撞车。** 小助手把空闲 30 分钟的 Claude 进程收起时，正好来一条消息，就会写进
  已关闭的管道，报 `ConnectionResetError('Connection lost')`。现在正在收起的进程不算活着，碰上了会
  等它退出、重新起一个再发。
- **项目路径跟主机走。** 老版本按“全局上次项目”记，换一台电脑新建会话时会带上另一台的路径
  （`/Volumes/Data-External/...`），报出一串路径。现在先确认这台电脑上有这个文件夹，没有就换成这台
  最近用过的项目；报错也写成“这台电脑上没有这个文件夹：…”。
- 小助手托着手机正在跑的会话时不能重启（`steerd.py stop` 会拒绝），新版要等这边空闲才换上。
- **选项目时能新建文件夹。** 原来浏览目录只能挑已有的，想开个新项目得先回电脑上建。现在每一层都有
  “在这里新建文件夹…”，建好直接用它开始会话（小助手新增 `dirs.create`）。浏览从 home 开始，也能往上走。
  搜索框里输一个还不存在的路径，会先问要不要新建，打错字不会悄悄多出一个文件夹。

## 第六轮（2026-11-12）：Claude 也像 Codex 那样两边同步

- **为什么要做。** 手机上一直提示"会放回 herdr"，用户问为什么。原因是 Claude 没有第二个客户端接入的协议，
  官方 `claude` 开着的会话手机只能"关掉那边、接着跑、做完放回"，而能放回的只有 herdr / tmux 窗格。
  用户要的是 Codex 那种效果：一个后台进程，终端和手机都是它的客户端。
- **试过但没用的路：Claude Code 自己的会话间消息通道。** 2.1.322 的交互式会话在
  `~/.claude/sessions/<pid>.json` 里登记了 `messagingSocketPath`（`/tmp/cc-socks/<pid>.sock`），
  带上 key 文件里的令牌能把 `{"type":"user",…}` 送进正开着的终端会话。实测送得进去，但它被包成
  "另一个 Claude 会话发来的，不是你的用户打的"，Claude 当成同事的请求、不会因此改设置，
  而 bypass 模式下每条都被扣住、要有人在电脑前点放行（除非把 `crossSessionInbound` 设成 `accept`，
  等于给本机所有同用户进程开门）。授权弹窗仍只能在电脑上点，接口也没公开。放弃。
- **做法：`steer` 终端界面。** 主机助手本来就是常驻后台、多客户端广播的，App 走的是它的 Unix socket。
  新增 `steerd.py claude`（`install-cli` 装成 `steer`），讲同一套协议，`hello` 里声明自己是
  `terminal`。它开的会话由小助手的 `claude -p` 进程跑，手机和终端都是观看者：两边的消息、流式回复、
  授权和提问同时出现，谁先回答算谁的；退出终端会话不停，手机接着用。不依赖 herdr，任何终端都行。
- **终端界面怎么画。** 不用全屏：写完的内容直接进终端自己的滚动历史，只重画底部（还在变的回复最后一行、
  正在跑的工具、排队的消息、请求、状态行、输入框）。中文按两列宽算换行；输入支持多行、粘贴、历史、
  `/` 命令补全、Shift+Tab 换权限模式、Esc 打断、Tab 采用 Claude 猜的下一句。请求出现后 0.4 秒内的按键不算，
  免得正在打字时误点了"允许"。
- **小助手的改动。** 用户消息带 `origin`（`phone` / `terminal`），只有手机发起的那一轮推送；会话摘要多了
  `terminals`（有几个 `steer` 开着它），App 据此显示"电脑终端里也开着这个会话，两边实时同步"，电脑上发的
  消息气泡下标"电脑上发的"；有 `steer` 开着时不再往 herdr / tmux 窗格里放回。
- **顺手修的两个老问题。** 运行中插进来的消息，Claude 现在会并进当前这一轮处理，只出一个 result；
  小助手原先以为它会自成下一轮，状态一直卡在"运行中"。现在用 `--replay-user-messages` 的回传判断它什么时候被收下，
  记录文件里这类消息（`attachment` / `queued_command`）也能还原出来。另外旧的小助手退出时会删掉 socket 路径，
  而这时新的可能已经在同一路径上起来了，结果新的连不上；现在只删自己的那个，且一停止监听就删。

