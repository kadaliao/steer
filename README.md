# Steer

在 iPhone 上用 Claude Code 和 Codex 写代码（vibecoding）。

Steer 通过 SSH 登录你的电脑，在那里运行 Claude Code / Codex，手机上是和官方 App 一样的对话界面：
回复逐字流出，工具步骤折叠成一行，授权、提问、计划都是拇指够得着的大按钮，输入用系统键盘
（你自己的输入法和语音听写都能用）。没有终端，没有中继。

- 首页：按时间排的会话，“需要你”的置顶；电脑上用过的 Claude / Codex 会话也在，点进去接着聊。
- 新建：点 ＋，打字，发送就开始。项目和 agent 默认上次用的。
- 对话：快捷短语一点就发，`@` 搜项目文件，`/` 选命令，可以发截图；运行中照样能发消息。
- 断线：agent 在电脑上继续跑，回到 App 自动补齐。

设计和取舍见 [docs/design.md](docs/design.md)，App 与主机助手之间的协议见
[docs/helper-protocol.md](docs/helper-protocol.md)。

## 电脑这边需要什么

- 打开 SSH（macOS：系统设置 › 通用 › 共享 › 远程登录）。
- `python3`（macOS 自带的 3.9 就行）和 `claude` 和/或 `codex`。
- 第一次连接时 App 会把主机助手 `steerd.py` 装到 `~/.local/share/steer/`；
  在 macOS 上它注册为 LaunchAgent `com.kadaliao.steer.helper`，这样 Claude 能读到钥匙串里的登录。
  卸载：`launchctl bootout gui/$(id -u)/com.kadaliao.steer.helper`，然后删除
  `~/Library/LaunchAgents/com.kadaliao.steer.helper.plist` 和 `~/.local/share/steer`。

## 开发

```sh
make build        # Simulator 构建（会先用 xcodegen 生成工程）
make test         # App 单元测试
make helper-test  # 主机助手单元测试
make run-sim      # 装到 Simulator 并启动
```

端到端 UI 测试会真的调用 Claude / Codex（用最小的模型，每次几分钱），需要 Simulator 能 SSH 到的主机：

```sh
TEST_RUNNER_STEER_UI_HOST=you@127.0.0.1:22 TEST_RUNNER_STEER_UI_PROJECT=/path/to/throwaway/project \
  xcodebuild -project Steer.xcodeproj -scheme SteerUI \
  -destination 'platform=iOS Simulator,name=iPhone Air' test
```

Simulator 里 App 的公钥可以这样拿到：
`xcrun simctl launch --console-pty booted com.kadaliao.steer -steerPrintKey`。

## 许可

Apache-2.0。`Packages/HeelerSSH` 和部分密钥代码来自 [Heeler](https://github.com/ZingerLittleBee/Heeler)，见 NOTICE。
