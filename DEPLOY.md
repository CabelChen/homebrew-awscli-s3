# awscli Agent 一键部署

在准备运行 Agent 的服务器用户下执行。支持 Linux glibc 的 x86_64/ARM64 和 macOS；
用 `/etc/os-release` 识别 Linux 用户空间，容器里的宿主机内核标识不参与包管理器选择。
缺少合适的 Python 时，自动准备用户目录内的 Python 3.11；S3 传输缺少官方 AWS CLI 时，
使用官方安装器准备用户级 AWS CLI v2。Linux 下载包要求 GPG 验签。
macOS 自动准备 Python 的 uv 路径要求 macOS 13+；已有 Python 3.10+ 时可直接复用。

## 一条命令在线部署

```sh
curl -fsSL https://raw.githubusercontent.com/CabelChen/homebrew-awscli-s3/main/deploy.sh -o deploy.sh && sh deploy.sh --store s3://YOUR_BUCKET/awscli-channel --name server-a
```

`store` 是任务通道，客户端和 Agent 必须使用相同前缀与 Agent 名称。
它使用目标服务器当前用户的 AWS profile、环境或 IAM Role；部署脚本不接收、不打包、不保存密钥。
使用已有 profile 时加 `--profile your-profile`，指定区域时加 `--region ap-southeast-1`。
S3 兼容服务可加 `--endpoint https://your-endpoint`。

已有有效配置时可直接运行 `sh deploy.sh`；首次无配置的交互终端会询问任务通道。
非交互运行需填写 `--store`。首次通过一行在线命令启动需要已有 curl；缺少 curl 的系统
可先复制 `deploy.sh` 到服务器再运行，脚本会通过已识别系统的包管理器准备 curl 和证书。
系统依赖安装使用 root 或可用的免交互 `sudo -n`；没有权限会明确退出，不改变 sudo 设置。

## 使用已经下载的安装包

```sh
sh deploy.sh --package ./awscli-agent.tar.gz --store s3://YOUR_BUCKET/awscli-channel --name server-a
```

默认下载已发布的 0.4.1 Agent 包，并核对内置 SHA256 与包内 MANIFEST。
自定义在线安装包必须加 `--sha256`；本地包也会检查成员白名单、路径、链接和 MANIFEST。
部署组件自身有固定 SHA256，下载不一致时立即停止。
下载安装包、Python 和 AWS CLI 需要能访问对应的 GitHub、Astral 和 AWS 官方下载地址。

## 启动与升级

默认 `--start auto` 优先使用可用的用户级 systemd，容器或没有用户 systemd 的环境使用后台进程。
可指定 `--start background` 或 `--start foreground`，只安装时使用 `--no-start`。
`--no-start` 仍会检查任务通道；部署需要读、写、列出、删除及 S3 条件写入权限。
探测只创建和删除随机临时对象，不下发执行任务。
macOS 默认后台运行；该模式不提供重启后自动拉起。
容器停止时其中的 Agent 也会停止。注销后保持用户 systemd 运行，需要管理员为该用户配置 lingering。

重复运行相同部署命令会保留已有配置、`owner.json` 和任务记录。
显式指定的 `store/profile/endpoint` 才更新对应配置，更新前创建受限权限的备份。
脚本只停止身份校验通过的受管进程；手动启动的 Agent 或自定义用户服务需先手动停止。
已有完整 `awscli` 客户端不会被 Agent 安装器替换；部署启动器直接运行新安装的 Agent 模块。
同一工作目录已有部署不能更换 Agent 名称；不同服务器使用不同名称。
原来使用自定义工作目录时，加 `--work-dir /原来的目录`，保留其中的 `owner.json` 和任务记录。

配置：`~/.config/s3-agent/config.ini`。
工作目录：`~/.local/state/awscli-agent`；存在旧 `~/.local/state/s3a-agent` 且新目录不存在时沿用旧目录。
执行日志：工作目录中的 `agent.log` 与各任务日志、结果文件；任务默认保留 7 天。
部署日志：`~/.local/state/awscli-deploy/deploy.log`。
部署状态和 PID：`~/.local/state/awscli-deploy/deployment.ini`，权限为 0600。

```sh
sh deploy.sh --help
# 用户 systemd 模式日志
journalctl --user -u awscli-agent -f
```

`--no-deps` 会在缺少依赖时退出。已有 Python 可通过环境变量指定：

```sh
AWSCLI_PYTHON=/usr/bin/python3.11 sh deploy.sh --store s3://YOUR_BUCKET/awscli-channel --name server-a
```
