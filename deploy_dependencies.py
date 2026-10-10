"""User-local AWS CLI bootstrap for the Agent deployment coordinator.

Only official installers and the operating system's configured package
repositories are used. Importing this module never installs anything.
"""

import json
import os
import platform
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


AWS_INSTALLER_URL = "https://awscli.amazonaws.com/v2/install.sh"
COMMAND_TIMEOUT = 30
PACKAGE_TIMEOUT = 600
INSTALL_TIMEOUT = 900
DOWNLOAD_TIMEOUT = 30
MAX_INSTALLER_BYTES = 2 * 1024 * 1024


class DependencyError(RuntimeError):
    """A deployment dependency failed without exposing command output."""


def _step(message):
    print(f"[部署] {message}", flush=True)


def _environment():
    environment = os.environ.copy()
    environment.update(AWS_PAGER="", AWS_CLI_AUTO_PROMPT="off", NO_COLOR="1")
    return environment


def _run(command, timeout=COMMAND_TIMEOUT, environment=None):
    """Capture subprocess output; callers never echo untrusted stderr."""
    try:
        return subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace", timeout=timeout,
            env=environment or _environment(), check=False,
        )
    except subprocess.TimeoutExpired:
        raise DependencyError("依赖命令超时；请检查网络后重试。") from None
    except (OSError, ValueError):
        raise DependencyError("无法启动依赖命令；请检查程序路径与执行权限。") from None


def _os_release():
    values = {}
    try:
        contents = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return values
    for line in contents.splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key in {"ID", "ID_LIKE", "VERSION_ID"}:
            values[key] = value.strip().strip("\"'")
    return values


def _linux_glibc():
    try:
        value = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (ValueError, OSError):
        value = ""
    match = re.fullmatch(r"glibc (\d+)\.(\d+)(?:\.\d+)?", value)
    if not match:
        name, version = platform.libc_ver()
        match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", version) if name == "glibc" else None
        if not match:
            raise DependencyError("AWS CLI v2 需要 glibc Linux；Alpine/musl 暂不支持自动部署。")
        return tuple(map(int, match.groups()[:2]))
    return tuple(map(int, match.groups()[:2]))


def _platform():
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux":
        if machine not in {"x86_64", "aarch64", "arm64"}:
            raise DependencyError("AWS CLI v2 自动部署仅支持 Linux x86_64 和 ARM64。")
        if _os_release().get("ID") == "alpine" or _linux_glibc() < (2, 17):
            raise DependencyError("AWS CLI v2 自动部署需要 glibc 2.17+；Alpine/musl 暂不支持。")
        return system
    if system == "Darwin":
        if machine not in {"x86_64", "arm64", "aarch64"}:
            raise DependencyError("AWS CLI v2 自动部署仅支持 macOS Intel 和 Apple Silicon。")
        version = platform.mac_ver()[0]
        if version and int(version.split(".", 1)[0]) < 11:
            raise DependencyError("AWS CLI v2 自动部署需要 macOS 11+。")
        return system
    raise DependencyError("Agent 自动部署目前仅支持 Linux 和 macOS。")


def _package_manager():
    release = _os_release()
    identifier = release.get("ID", "")
    related = set(release.get("ID_LIKE", "").split()) | {identifier}
    if related & {"debian", "ubuntu"}:
        manager, family = "apt-get", "apt"
    elif identifier == "amzn":
        manager = "dnf" if release.get("VERSION_ID", "").startswith("2023") else "yum"
        family = "rpm"
    elif related & {"rhel", "centos", "fedora", "rocky", "almalinux"}:
        manager, family = ("dnf" if shutil.which("dnf") else "yum"), "rpm"
    else:
        raise DependencyError("无法识别系统包管理器；请手动安装缺少的依赖后使用 --no-deps。")
    executable = shutil.which(manager)
    if not executable:
        raise DependencyError(f"系统未提供 {manager}；请手动安装缺少的依赖后使用 --no-deps。")
    return executable, family


def _privileged_prefix():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    sudo = shutil.which("sudo")
    if sudo and _run([sudo, "-n", "true"]).returncode == 0:
        return [sudo, "-n"]
    raise DependencyError("安装系统依赖需要 root 或已授权的免交互 sudo；请管理员安装依赖后重试。")


def ensure_tools(required=None, no_deps=False):
    """Return absolute paths, installing missing known system tools if allowed.

    Linux defaults to curl/bash/unzip/gpg and macOS to curl/bash/installer/
    pkgutil. No repository, shell profile, credential, or sudo setting changes.
    """
    system = _platform()
    required = tuple(required if required is not None else (
        ("curl", "bash", "unzip", "gpg") if system == "Linux"
        else ("curl", "bash", "installer", "pkgutil")
    ))
    supported = {"curl", "bash", "unzip", "gpg", "installer", "pkgutil", "tar", "gzip"}
    if any(command not in supported for command in required):
        raise DependencyError("依赖列表包含不支持自动安装的程序。")
    paths = {command: shutil.which(command) for command in required}
    missing = [command for command, path in paths.items() if not path]
    if not missing:
        return paths
    names = ", ".join(missing)
    if no_deps:
        raise DependencyError(f"缺少依赖：{names}；--no-deps 禁止自动安装。")
    if system == "Darwin":
        brew = shutil.which("brew")
        if missing != ["gpg"] or not brew:
            raise DependencyError(f"缺少依赖：{names}；请安装系统工具或使用已有 Homebrew 安装。")
        _step("使用已有 Homebrew 安装 GnuPG")
        environment = _environment()
        environment.update(HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_NO_AUTOREMOVE="1")
        result = _run([brew, "install", "gnupg"], PACKAGE_TIMEOUT, environment)
    else:
        if any(command in {"installer", "pkgutil"} for command in missing):
            raise DependencyError("Linux 不提供 macOS 安装工具。")
        manager, family = _package_manager()
        prefix = _privileged_prefix()
        package_names = {"gpg": "gnupg" if family == "apt" else "gnupg2"}
        packages = list(dict.fromkeys(package_names.get(command, command) for command in missing))
        # Downloads require trusted HTTPS certificates even in minimal images.
        if "ca-certificates" not in packages:
            packages.append("ca-certificates")
        _step(f"使用系统仓库安装缺少的依赖：{names}")
        if family == "apt":
            environment = _environment()
            environment["DEBIAN_FRONTEND"] = "noninteractive"
            updated = _run([*prefix, manager, "update"], PACKAGE_TIMEOUT, environment)
            if updated.returncode:
                raise DependencyError("系统软件源更新失败；请检查仓库与网络配置。")
            result = _run([*prefix, manager, "install", "-y", *packages], PACKAGE_TIMEOUT, environment)
        else:
            result = _run([*prefix, manager, "install", "-y", *packages], PACKAGE_TIMEOUT)
    if result.returncode:
        raise DependencyError("系统依赖安装失败；请检查包管理器、仓库与可用磁盘空间。")
    paths = {command: shutil.which(command) for command in required}
    missing = [command for command, path in paths.items() if not path]
    if missing:
        raise DependencyError("依赖安装后仍缺少程序：" + ", ".join(missing) + "；请管理员检查包安装结果。")
    return paths


def _supports_conditional_put(executable):
    try:
        # This is a local service-model check. No credentials, instance
        # metadata, or caller profile are needed to generate an input skeleton.
        environment = _environment()
        environment.update(
            AWS_EC2_METADATA_DISABLED="true", AWS_CONFIG_FILE=os.devnull,
            AWS_SHARED_CREDENTIALS_FILE=os.devnull,
        )
        for key in (
            "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
            "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN",
        ):
            environment.pop(key, None)
        result = _run([
            str(executable), "s3api", "put-object", "--generate-cli-skeleton", "input",
            "--no-sign-request", "--region", "us-east-1",
        ], environment=environment)
        if result.returncode:
            return False
        value = json.loads(result.stdout)
        return isinstance(value, dict) and "IfNoneMatch" in value
    except (DependencyError, ValueError, TypeError):
        return False


def _managed_destination(bin_path, root, system):
    """Refuse to overwrite unrelated launchers or installation directories."""
    expected = root / ("v2/current/bin/aws" if system == "Linux" else "aws")
    if not expected.resolve().is_relative_to(root.resolve()):
        raise DependencyError("AWS CLI 安装目录内部链接指向外部位置，拒绝覆盖。")
    for name in ("aws", "aws_completer"):
        path = bin_path / name
        if os.path.lexists(path):
            target = expected if name == "aws" else expected.with_name("aws_completer")
            if not path.is_symlink() or path.resolve() != target.resolve():
                raise DependencyError(f"用户 bin 目录已有非受管 {name}，拒绝覆盖；请调整路径后重试。")
    if root.is_symlink():
        raise DependencyError("AWS CLI 安装目录是外部链接，拒绝覆盖。")
    if root.exists():
        if not root.is_dir() or not expected.is_file():
            raise DependencyError("AWS CLI 安装目录已有无法确认归属的内容，拒绝覆盖。")
        probe = _run([str(expected), "--version"])
        if probe.returncode or not re.match(r"aws-cli/2\.\d+\.\d+\b", probe.stdout.strip()):
            raise DependencyError("已有安装目录无法确认为官方 AWS CLI v2，拒绝覆盖。")


class _OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        parsed = urllib.parse.urlsplit(new_url)
        if parsed.scheme != "https" or parsed.netloc != "awscli.amazonaws.com":
            raise DependencyError("AWS 安装脚本下载发生非官方跳转，已停止。")
        return super().redirect_request(request, response, code, message, headers, new_url)


def _download_installer(path):
    parsed = urllib.parse.urlsplit(AWS_INSTALLER_URL)
    if parsed.scheme != "https" or parsed.netloc != "awscli.amazonaws.com":
        raise DependencyError("AWS 安装脚本地址必须是官方 HTTPS 地址。")
    try:
        opener = urllib.request.build_opener(_OfficialRedirect())
        deadline = time.monotonic() + DOWNLOAD_TIMEOUT
        with opener.open(AWS_INSTALLER_URL, timeout=DOWNLOAD_TIMEOUT) as response:
            pieces = []
            size = 0
            while size <= MAX_INSTALLER_BYTES:
                if time.monotonic() >= deadline:
                    raise DependencyError("下载 AWS 官方安装脚本超时；请检查网络后重试。")
                piece = response.read1(min(65536, MAX_INSTALLER_BYTES + 1 - size))
                if not piece:
                    break
                pieces.append(piece)
                size += len(piece)
            contents = b"".join(pieces)
        if len(contents) > MAX_INSTALLER_BYTES or not contents.startswith(b"#!/usr/bin/env bash"):
            raise DependencyError("AWS 安装脚本内容异常，已停止。")
        path.write_bytes(contents)
        path.chmod(0o600)
    except DependencyError:
        raise
    except (urllib.error.URLError, OSError, ValueError):
        raise DependencyError("下载 AWS 官方安装脚本失败；请检查 HTTPS 网络和证书。") from None


def _installation_error(stderr):
    text = stderr.lower()
    if any(word in text for word in ("signature", "signed", "verification", "gpg")):
        return "AWS 安装包签名验证失败；已停止安装，请检查 GnuPG 与官方网络下载。"
    if any(word in text for word in ("certificate", "ssl", "tls")):
        return "AWS 安装包 HTTPS 证书检查失败；请检查系统时间与 CA 证书。"
    if any(word in text for word in ("curl", "download", "resolve", "connect", "network")):
        return "AWS 安装包下载失败；请检查网络后重试。"
    if any(word in text for word in ("permission", "denied", "whitespace")):
        return "AWS 用户级安装路径不可写或包含空白；请检查用户目录。"
    return "AWS 官方安装器执行失败；请检查依赖、用户目录与可用磁盘空间。"


def ensure_aws(no_deps=False):
    """Reuse a capable AWS CLI or install verified v2 for the current user."""
    system = _platform()
    existing = shutil.which("aws")
    if existing and _supports_conditional_put(existing):
        _step("复用已支持 S3 条件写入的 AWS CLI")
        return os.path.abspath(existing)
    if no_deps:
        raise DependencyError("缺少支持 IfNoneMatch 的 AWS CLI；--no-deps 禁止自动安装。")
    bin_path = Path.home() / ".local/bin"
    root = Path.home() / ".local/share/aws-cli"
    _managed_destination(bin_path, root, system)
    tools = ensure_tools(no_deps=no_deps)
    _step("安装官方 AWS CLI v2 到当前用户目录并校验安装包签名")
    environment = _environment()
    environment.update(XDG_DATA_HOME=str(root.parent), XDG_BIN_HOME=str(bin_path))
    environment.pop("AWS_CLI_DISTRIBUTION_SOURCE_OVERRIDE", None)
    with tempfile.TemporaryDirectory(prefix="awscli-deploy-aws-") as temporary:
        installer = Path(temporary) / "install.sh"
        _download_installer(installer)
        result = _run([tools["bash"], str(installer), "--quiet"], INSTALL_TIMEOUT, environment)
        if result.returncode:
            raise DependencyError(_installation_error(result.stderr))
        # The official Linux script permits skipping GPG when absent; our
        # bootstrap requires it and rejects an unexpected verification skip.
        if system == "Linux" and "skipping signature verification" in result.stderr.lower():
            raise DependencyError("AWS 安装器未完成 PGP 签名验证，已停止部署。")
    executable = bin_path / "aws"
    if not executable.is_file() or not _supports_conditional_put(executable):
        raise DependencyError("AWS 安装后未通过 S3 IfNoneMatch 能力检查，Agent 未启动。")
    previous = os.environ.get("PATH", "")
    os.environ["PATH"] = str(bin_path) + (os.pathsep + previous if previous else "")
    return str(executable.absolute())
