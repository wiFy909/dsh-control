# DSH Control v0.3.0

省心接管 DeepSeek Harness 的日常启停、监控与更新。已有 DSH 环境沿用原 Key、配置与会话；全新环境安装官方包，在网页中输入 Key。用量、估算金额、余额和高低峰时段集中显示。

## 一行命令安装

先安装 Node.js 24+（含 npm），重新打开终端。Windows PowerShell / cmd、macOS、Linux 使用同一条命令：

```sh
npx --yes https://github.com/wiFy909/dsh-control/releases/latest/download/dsh-control-installer.tgz
```

安装器识别系统，校验下载包，并准备 Control 和官方 DSH 环境；Python 缺失时会自动准备。需要联网，无须管理员权限。命令在本 Release 发布并包含对应附件后生效。

## 下载安装包

| 系统 | 附件 | 解压后操作 |
|---|---|---|
| macOS | dsh-control-macos.zip | 双击 Install DSH Control.command，或运行 `sh install.sh` |
| Windows | dsh-control-windows.zip | 双击 Install DSH Control.cmd |
| Linux / WSL | dsh-control-linux.zip | 在终端运行 `sh install.sh` |

三个包是使用同一代码的联网安装包，运行环境由安装器按平台准备，并非免依赖的原生单文件程序。Node.js 24+ 仍需事先安装。Mac 有实机使用证据；Windows/Linux 原生桌面与各 CPU 架构仍需实机验证。

安装后重新打开终端，输入 `dsh-control`。若 DSH 接入未完成，按错误提示处理，再运行 `dsh-control --setup`。已有网页版服务请先从原入口停止；安装器读取 `DSH_HOME` 或默认 `~/.dsh`，不会终止原进程。自定义目录使用 `dsh-control --setup --dsh-home "/原DSH绝对路径"`。

余额使用原 DSH Key 或手动配置的官方 Key 查询；用量与金额来自本地日志及估算，不能代替账单。Key 仅通过环境变量提供时，需从具有相同变量的终端启动 Control；原工作目录 `.env` 中的 Key 请先在官方网页保存。

## 校验与更新

附件 `SHA256SUMS` 包含下载文件的 SHA-256。macOS 使用 `shasum -a 256 包名.zip`，Linux 使用 `sha256sum 包名.zip`，Windows 使用 `Get-FileHash 包名.zip -Algorithm SHA256` 核对。

更新 Control：先停止服务并退出，再重新安装新 Release。更新 DSH：在控制台点击“5 更新”，跟随官方版本提示安装，保留原 HOME。

您的最佳 DeepSeek Harness 启停与监控管家！
