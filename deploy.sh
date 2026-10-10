#!/bin/sh
# Bootstrap only: the verified Python coordinator performs deployment.
set -eu
umask 077

BASE_URL='https://raw.githubusercontent.com/CabelChen/homebrew-awscli-s3/main'
COORDINATOR_SHA256='67c487a6799e10ec6bf684b3dfedb1c5f644b8c811bbb96626e8d4dd53e8371a'
DEPENDENCIES_SHA256='6dc15b5f2b3fae4d65fd93a1aab5f840159043b8cdc606ffa31aef9f7f8861a3'

say() { printf '%s\n' "$*"; }
die() { say "部署失败：$*" >&2; exit 1; }

show_help() {
    cat <<'HELP'
awscli Agent 一键部署（Linux / macOS，Python 3.10+ 自动准备）

用法：sh deploy.sh [选项]
  --store URI       与客户端相同的 s3://桶/任务通道；未填则沿用已有配置
  --name NAME       Agent 名称，默认 server-a
  --work-dir DIR    持久工作目录；升级可指定原目录以保留任务身份
  --profile NAME    服务器上已有的 AWS profile；IAM Role 可省略
  --endpoint URL    自定义 S3 服务地址
  --region REGION   AWS 区域
  --package PATH    已下载的安装包，或 https:// / s3:// 地址
  --sha256 HASH     自定义安装包的预期 SHA256
  --start MODE      auto（默认）/ service / background / foreground
  --no-start        仅安装和配置
  --no-deps         缺少依赖时退出，不自动安装
  --help            显示本帮助，不下载或安装

支持 Ubuntu/Debian、Rocky/Alma/RHEL/CentOS、Amazon Linux、macOS。
没有 systemd 的容器自动使用后台进程；不修改系统默认 Python 或 shell 配置。
AWS 凭据使用当前用户已有 profile / IAM Role，不接收或保存密钥。
可用 AWSCLI_PYTHON=/绝对路径 指定已有 Python 3.10+。
HELP
}

NO_DEPS=0
EXPECT_VALUE=0
for argument in "$@"; do
    if [ "$EXPECT_VALUE" = 1 ]; then EXPECT_VALUE=0; continue; fi
    case "$argument" in
        --help|-h) show_help; exit 0 ;;
        --no-deps) NO_DEPS=1 ;;
        --no-start) ;;
        --store|--name|--work-dir|--profile|--endpoint|--region|--package|--sha256|--start) EXPECT_VALUE=1 ;;
        --store=*|--name=*|--work-dir=*|--profile=*|--endpoint=*|--region=*|--package=*|--sha256=*|--start=*) ;;
        *) die "未知参数；请运行 sh deploy.sh --help" ;;
    esac
done
[ "$EXPECT_VALUE" = 0 ] || die '参数缺少值；请运行 sh deploy.sh --help'

SYSTEM=$(uname -s)
case "$SYSTEM" in Linux|Darwin) ;; *) die '仅支持 Linux 和 macOS' ;; esac
MACHINE=$(uname -m)
case "$MACHINE" in x86_64|aarch64|arm64) ;; *) die '仅支持 x86_64 / ARM64' ;; esac
if [ "$SYSTEM" = Linux ]; then
    [ -r /etc/os-release ] || die '缺少 /etc/os-release，无法确定当前系统'
    # Read data, never source a distribution-owned file as shell code.
    DISTRO=$(sed -n 's/^ID=//p' /etc/os-release | tr -d '"' | tr -d "'")
    say "系统：$DISTRO / $MACHINE"
    [ "$DISTRO" != alpine ] || die 'Alpine/musl 暂不支持官方 AWS CLI；请使用 glibc 系统镜像'
else
    DISTRO=macos
    say "系统：macOS / $MACHINE"
fi

PYTHON=''
if [ -n "${AWSCLI_PYTHON:-}" ]; then
    [ -x "$AWSCLI_PYTHON" ] || die 'AWSCLI_PYTHON 指向的解释器不可执行'
    "$AWSCLI_PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die '需要 Python 3.10+'
    PYTHON=$AWSCLI_PYTHON
else
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
            PYTHON=$(command -v "$candidate")
            break
        fi
    done
fi

TMP_DEPLOY=$(mktemp -d "${TMPDIR:-/tmp}/awscli-deploy.XXXXXXXX")
trap 'rm -rf "$TMP_DEPLOY"' EXIT HUP INT TERM

if ! command -v curl >/dev/null 2>&1; then
    [ "$NO_DEPS" = 0 ] || die '缺少 curl；--no-deps 禁止自动准备依赖'
    [ "$SYSTEM" = Linux ] || die '缺少系统 curl，请先安装 curl'
    say '准备 curl 和 CA 证书'
    PRIVILEGE=''
    if [ "$(id -u)" != 0 ]; then
        command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1 || die '缺少 curl，请管理员先安装 curl ca-certificates'
        PRIVILEGE='sudo -n'
    fi
    case "$DISTRO" in
        ubuntu|debian|linuxmint|pop)
            $PRIVILEGE env DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::Retries=2 update -qq >"$TMP_DEPLOY/bootstrap.log" 2>&1 || die '系统软件源更新失败，请检查 apt 软件源和网络'
            $PRIVILEGE env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates >>"$TMP_DEPLOY/bootstrap.log" 2>&1 || die 'curl 和证书安装失败，请检查 apt 软件源、权限和磁盘空间'
            ;;
        amzn|rhel|centos|rocky|almalinux|fedora|ol)
            if command -v dnf >/dev/null 2>&1; then MANAGER=dnf; else MANAGER=yum; fi
            command -v "$MANAGER" >/dev/null 2>&1 || die '当前镜像没有 dnf/yum，请管理员先安装 curl'
            $PRIVILEGE "$MANAGER" install -y curl ca-certificates >"$TMP_DEPLOY/bootstrap.log" 2>&1 || die 'curl 和证书安装失败，请检查软件源、权限和磁盘空间'
            ;;
        *) die '无法自动准备此系统的 curl，请先安装 curl ca-certificates' ;;
    esac
fi

fetch() {
    curl --proto '=https' --tlsv1.2 -fLsS --connect-timeout 15 --max-time 180 --retry 2 "$1" -o "$2"
}

if [ -z "$PYTHON" ]; then
    [ "$NO_DEPS" = 0 ] || die '未找到 Python 3.10+；--no-deps 禁止自动准备依赖'
    say '准备独立 Python 3.11（uv），保留系统 Python'
    UV_DIR="$HOME/.local/share/awscli-deploy/bin"
    if [ ! -x "$UV_DIR/uv" ]; then
        fetch https://astral.sh/uv/install.sh "$TMP_DEPLOY/uv-install.sh"
        UV_INSTALL_DIR="$UV_DIR" UV_NO_MODIFY_PATH=1 sh "$TMP_DEPLOY/uv-install.sh"
    fi
    export UV_PYTHON_INSTALL_DIR="$HOME/.local/share/awscli-deploy/python"
    "$UV_DIR/uv" --no-config python install --no-bin 3.11
    PYTHON=$("$UV_DIR/uv" --no-config python find --managed-python --system 3.11)
fi
"$PYTHON" -c 'import sys; print("Python：" + sys.version.split()[0]); sys.exit(sys.version_info < (3, 10))' || die '需要 Python 3.10+'

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for component in deploy_agent.py deploy_dependencies.py; do
    if [ -f "$SCRIPT_DIR/$component" ] && [ ! -L "$SCRIPT_DIR/$component" ]; then
        cp "$SCRIPT_DIR/$component" "$TMP_DEPLOY/$component"
    else
        fetch "$BASE_URL/$component" "$TMP_DEPLOY/$component"
    fi
done

"$PYTHON" - "$TMP_DEPLOY" "$COORDINATOR_SHA256" "$DEPENDENCIES_SHA256" <<'PY'
import hashlib, pathlib, sys
folder = pathlib.Path(sys.argv[1])
for name, expected in zip(('deploy_agent.py', 'deploy_dependencies.py'), sys.argv[2:]):
    if hashlib.sha256((folder / name).read_bytes()).hexdigest() != expected:
        raise SystemExit('部署组件校验失败，请重新下载 deploy.sh')
PY

export PATH="$HOME/.local/bin:$PATH"
"$PYTHON" "$TMP_DEPLOY/deploy_agent.py" "$@"
