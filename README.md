# DSH Control

DeepSeek Harness（DSH）的第三方终端控制台。用 `dsh-control` 启停服务、打开聊天网页、查看运行状态和用量。

## 快速开始

先安装 [Node.js 24 或更新版本](https://nodejs.org/en/download)（含 npm）。Windows 使用 [PowerShell 7](https://www.microsoft.com/store/apps/9MZ1SNWT0N5D)。使用 WSL 时，请在 WSL 的 Linux 终端内安装 Node.js 并执行下方命令。

> [!IMPORTANT]
> **如果因网络问题无法拉取 GitHub 仓库，或下载安装脚本时超时、卡住，请先连接能访问 GitHub 的 VPN，再在终端中重试。**

**Windows（PowerShell 7）**

```powershell
irm https://raw.githubusercontent.com/wiFy909/dsh-control/main/bootstrap.ps1 | iex
```

**macOS / Linux / WSL**

```sh
curl -fsSL https://raw.githubusercontent.com/wiFy909/dsh-control/main/install.sh | sh
```

安装完成后，**关闭并重新打开终端**，在任意目录运行：

```sh
dsh-control
```

## 第一次使用

1. 看到鲸鱼画面后，按 **Enter** 进入。若出现安装引导，选择实际运行的系统，按提示在对应终端执行 `dsh-control --setup`，再回到引导页点击 **安装完毕**。
2. 点击 **1 启动**，浏览器会打开 DSH 聊天网页。首次安装 DSH 时，在网页中填写模型 API Key。接入已有 DSH 环境后可沿用原 Key。

## 每天怎么用

- **1 启动** 打开聊天网页。关闭网页后，点击 **3 重开网页** 即可继续。
- 用完点击 **2 停止**，等服务停止后点击 **6 退出**。
- 更新 DSH 时，先停止服务，再点击 **5 更新**，按界面提示完成安装并确认接入。

## 项目亮点

- **一处管理服务。** 启停、重开网页、自检和版本更新都在同一个终端界面，支持鼠标和快捷键。
- **沿用已有环境。** 接入已有 DSH 时，可保留原配置、Key 和会话。新装使用 DeepSeek 官方 DSH 包。
- **运行状态一屏可见。** 查看服务链路和各阶段耗时，搜索已加载的插件、技能和 MCP。
- **用量集中展示。** 底栏汇总本地会话的 Token 用量和估算金额，并显示账户余额和高低峰倒计时。

[MIT License](LICENSE)
