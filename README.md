<p align="center"><img src="assets/hero.svg" alt="DSH Control：让 DSH 的日常使用更简单" width="1080"></p>

<p align="center"><a href="#快速开始">快速开始</a> · <a href="#第一次使用">第一次使用</a> · <a href="#每天怎么用">日常操作</a> · <a href="#更新-dsh">更新 DSH</a> · <a href="https://github.com/wiFy909/dsh-control/releases">下载安装包</a></p>

**DSH Control 是 DeepSeek Harness 的终端控制台。** 安装一次，之后在任意目录输入 `dsh-control`，就能启动或停止服务、重新打开聊天网页、检查运行状态，以及查看插件、技能、MCP 和本地用量。

这是社区开发的独立工具，与 DeepSeek 官方没有隶属关系。实际聊天和任务执行仍由 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 完成。

## 亮点

- **一个入口，六个操作。** 启动、停止、重开网页、自检、更新、退出；鼠标点击和数字快捷键都能用。
- **打开就知道下一步。** 每次先显示鲸鱼欢迎画面，按 Enter、Space 或鼠标左键继续；已有有效绑定进入控制台，尚未安装则进入平台引导。
- **沿用数据，而不是重新开始。** 已绑定安装的 Key、配置和会话保存在原目录；更新 DSH 时继续使用同一目录。
- **用量来自本地记录。** 显示今日、近七日、记录日均与估算金额；小窗口缩写大额 Token 数，悬停可看完整数字。
- **普通终端就能用。** 默认用字符绘制鲸鱼，无需专用图像协议；界面适配 120×30，并提供 80×24 紧凑布局。

## 它解决哪些问题

![服务状态不清楚、用量难查看、升级步骤分散：DSH Control 的三个主要用途](assets/why.svg)

服务是否真的启动、网页为什么打不开，可以在“服务链路”和“结果与建议”中查看。用量不必手工汇总日志；升级也不必猜应该安装哪个版本。Control 把这些日常动作放在同一处，并保留失败原因和下一步提示。

## 快速开始

**Mac：** 打开“终端”，复制下面这一行，按回车。

```sh
curl -fsSL https://github.com/wiFy909/dsh-control/releases/latest/download/install.sh | sh
```

安装器会下载发行包并核对 SHA-256，在你的用户目录中准备独立运行环境。已有 Python 3.10+ 时直接复用；缺少时通过 [uv 官方安装器](https://docs.astral.sh/uv/reference/installer/)准备 Python 3.12。整个过程不需要 `sudo`。

看到“安装完成”后，**重新打开终端**，在任意目录输入：

```sh
dsh-control
```

这一步安装的是 **Control 控制台**。要运行 DSH 本身，还需要 Node.js 24+ 和 DSH 的模型配置；第一次打开时会继续引导你完成。

<details>
<summary>Windows、Linux、WSL 与手动下载</summary>

| 平台 | 安装方法 | 当前验证范围 |
|---|---|---|
| macOS | 上面的单行命令；也可下载 ZIP 后双击 `Install DSH Control.command` | Mac 实机使用和人工验收已通过 |
| Windows 原生 | 先安装 Python 3.10+，再运行下面的 PowerShell 命令 | 提供安装与运行路径，桌面体验仍处于预览阶段 |
| Ubuntu / Linux | 在终端执行与 Mac 相同的单行命令 | Ubuntu 为优先目标，实机体验仍处于预览阶段 |
| Windows / WSL | 在 Ubuntu 的终端中执行与 Mac 相同的命令 | 与 Windows 原生环境分别安装、分别管理；实机体验仍处于预览阶段 |

Windows PowerShell：

```powershell
irm https://github.com/wiFy909/dsh-control/releases/latest/download/install.ps1 | iex
```

需要手动下载时，进入 [Releases](https://github.com/wiFy909/dsh-control/releases/latest)，下载 **dsh-control.zip** 并解压。Mac 双击 `Install DSH Control.command`；Windows 双击 `Install DSH Control.cmd`；Linux / WSL 在解压目录执行 `sh install.sh`。

这是带自动安装器的源码发行包，安装时需要联网获取锁定的 Python 依赖；它不是免运行环境的原生单文件程序。安装后不必保留解压目录。

</details>

## 第一次使用

![从打开到退出的完整操作顺序](assets/workflow.svg)

1. 输入 `dsh-control`，看到鲸鱼后，按 **Enter** 或 **Space**，也可以单击鼠标左键。
2. 如果已经接入过可用的 DSH，直接进入控制台。否则选择你实际运行的系统：**Windows、Windows / WSL、Linux 或 Mac**。WSL 指 Ubuntu 等 Linux 环境，不能把 Windows 原生的 Node 当作 WSL 的 Node。
3. 按页面要求准备 [Node.js 24 或更新版本](https://nodejs.org/en/download)，然后复制页面给出的 DSH 安装命令，在相应终端执行。安装页使用经过适配的固定版本；它不会自动把 Node.js、WSL 或 API Key 一起装好。
4. 等待命令执行成功，回到 Control 点击 **安装完毕**。检查通过后进入控制台；失败时按页面的具体提示处理，再重试。
5. 点击 **1 启动**。服务和网页资源检查完成后，浏览器会打开 DSH。首次使用模型时，按 DSH 网页自身的引导配置模型与 API Key。

新安装的 DSH 程序和数据目录会在安装页面显示。Control 不会仅凭发现一个 `npx` 缓存就替你接管正在运行的服务。已有其他方式安装的 DSH，先停止原服务并确认其数据目录；不要为了接入而删除原来的 Key、配置或会话。

## 每天怎么用

| 操作 | 点击或按键 | 你会看到什么 |
|---|---|---|
| 开始使用 | **1 启动** | 检查环境和安装，启动服务，再打开聊天网页。服务已运行时复用现有进程。 |
| 结束服务 | **2 停止** | 正常停止受管进程；等待状态变为“已停止”。 |
| 找回网页 | **3 重开网页** | 重新打开正在运行的 DSH。关闭浏览器标签页不会停止服务。 |
| 排查故障 | **4 运行自检** | 查看环境、进程、Web 服务和访问链路；具体原因显示在“结果与建议”。 |
| 更新 DSH | **5 更新** | 查询官方版本，按平台生成明确版本号的更新命令。 |
| 退出 Control | **6 退出** | 服务已停止时返回终端；仍在运行时提醒先停止。`Q`、`Ctrl+C` 使用同一规则。 |

日常最常用的顺序是：**打开 Control → 1 启动 → 在浏览器里使用 DSH → 2 停止 → 6 退出**。

右侧可以切换 **插件、技能、MCP**，搜索当前列表并滚动查看详情。这部分用于浏览，不会因为点击条目就安装或删除扩展。小窗口中按 **V** 在服务链路和详情之间切换；按 **Tab** 切换焦点。

### 用量、金额和账户余额

底部自动读取**当前绑定的 DSH 数据目录**里的会话日志，显示今天、近七日和有记录日期的日均用量。万、亿缩写只是显示方式，鼠标悬停可查看完整数值。高峰倒计时使用北京时间。

**本地估算金额与账户余额是两回事。** 估算根据日志中的模型、缓存和时间信息计算，不能代替官方账单；缺少 usage、日志未落盘或模型价格未知时，会显示不完整提示。没有可读记录时显示“—”，不会把它当成零消费。当前内置价格日历覆盖 2026-08-17 至 2026-10-24，超出范围后继续显示 Token，金额标记为无法可靠估算。

点击左下角 **账户**（或按 **A**）可以配置余额查询。这里的 Key 只用于官方余额接口，**不会替 DSH 配置聊天模型**。“仅本次”保存在当前进程，“安全保存”使用系统密钥库；两者都有对应清除入口。余额请求会发送到 `api.deepseek.com`，会话正文不因本地统计而上传。

## 更新 DSH

1. 先点击 **2 停止**，等待“已停止”。
2. 点击 **5 更新**。Control 通过 HTTPS 查询官方 npm 包 `@deepseek-ai/dsh` 的 `latest`，显示当前版本、目标版本和查询时间。
3. 选择平台，复制命令，在对应终端执行。命令会把新版装到新的程序目录；原程序仍保留。Node 不满足新版要求时，先升级 Node，再重试。
4. 安装成功后回到 Control，点击 **更新完毕**。Control 核实版本、入口、锁文件和停止状态，再切换绑定；原数据目录、端口和已授权的凭据引用继续使用。
5. 点击 **1 启动**，使用新版。

查询失败时点击“重新查询”；下载安装或核实失败时，保留原绑定。官方 `latest` 也可能是 RC 预发布版，升级前可查看上游说明。上游自身的配置变更仍需要兼容性检查。

**更新 Control 自身：** 先停止服务并退出 Control，再重新执行“快速开始”的安装命令，或安装新的 Release ZIP。界面里的“5 更新”只更新 DSH。

## 常见问题

**输入 `dsh-control` 提示找不到命令？** 重新打开终端再试。Mac/Linux 的命令安装在 `~/.local/bin`；Windows 的命令目录会加入用户 PATH。安装时出现错误，则先处理错误并重新安装。

**我想直接输入 `dsh` 打开 Control。** 默认只注册 `dsh-control`，保留已有 `dsh` 命令。Mac/Linux 可在安装时加上明确选项：

```sh
curl -fsSL https://github.com/wiFy909/dsh-control/releases/latest/download/install.sh | sh -s -- --replace-dsh
```

这会替换用户命令目录中的 `dsh` 启动入口，并保留旧启动脚本备份。它不会卸载官方 DSH，也不会自动迁移其他管理器的服务。

**点击启动后没打开网页？** 查看“结果与建议”，再运行自检。若服务已经就绪，只是关掉了网页，点击“3 重开网页”。自检不会发起模型对话，也不能证明 Key 有效或余额充足。

**鲸鱼不完整或文字太挤？** 建议使用 120 列、30 行左右的终端窗口。80×24 下可按 V 切换内容区域；字体不支持细点阵时，运行 `dsh-control --glyph-mode block`。中文等宽字体会让边框更整齐。

**程序、数据保存在哪？** Mac/Linux 的 Control 程序和管理记录在 `~/.local/share/dsh-control`；Windows 的 Control 程序在 `%LOCALAPPDATA%/dsh-control/app`。DSH 的实际数据目录以当前绑定为准，界面里可以看到 HOME。更新 Control 不会覆盖这个目录。

**如何卸载？** 先停止服务并退出，再删除 Control 的用户程序目录与它创建的命令入口。若替换过 `dsh`，旧入口备份位于 Control 程序目录的 `previous-commands`。如需彻底清理，还可移除安装器追加的 PATH 行。删除 Control 与删除 DSH 数据是两件事；会话与 Key 请自行备份和决定是否保留。

## 从源码运行与参与开发

普通使用直接下载 Release 即可。需要修改代码时：

```sh
git clone https://github.com/wiFy909/dsh-control.git
cd dsh-control
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m dsh_control_app.app
```

Windows 将 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`。运行测试：`python -m unittest discover -s tests`；构建发行包：`python scripts/build-release.py`。测试使用隔离夹具，不需要真实 API Key。提交问题时请说明系统、终端和操作步骤，截图与日志先去掉 Key、用户名、主机名、个人路径和会话内容。

## 许可证与致谢

本项目采用 [MIT License](LICENSE)。鲸鱼标识来自 DeepSeek 官方资源，其来源和许可证保留在 [资源目录](dsh_control_app/assets/brand/PROVENANCE.txt)。DeepSeek Harness、Textual、Rich、Pillow 及其他依赖遵循各自许可证。
