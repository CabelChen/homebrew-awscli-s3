#!/usr/bin/env python3
"""Deploy the server-only awscli package without changing credentials or task state."""
import argparse
import configparser
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

PACKAGE_URL = 'https://github.com/CabelChen/homebrew-awscli-s3/releases/download/v0.4.1/awscli-agent-0.4.1.tar.gz'
PACKAGE_SHA256 = '1df61def29623751f1cb7fc8f52cfe055e5a0fd5c562281be36b6bf631a10efe'
PACKAGE_FILES = {'s3_agent.py', 's3_features.py', 'agent_entry.py', 'install_agent.py',
                 'install.sh', 'AGENT.md', 'config.example.ini', 'awscli-agent.service'}
MANAGED = 'awscli-deploy-v1'
SERVICE_MARKER = '# Managed by awscli deploy v1\n'
SERVICE_NAME = 'awscli-agent.service'
MAX_PACKAGE_BYTES = 64 * 1024 * 1024


class DeployError(RuntimeError):
    pass


class LockBusy(DeployError):
    pass


def checked_run(command, *, env=None, timeout=30, what='command'):
    """Never echo subprocess stderr, which can contain credentials or signed URLs."""
    try:
        result = subprocess.run(command, env=env, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeployError(f'{what}失败或超时（{type(error).__name__}）') from None
    if result.returncode:
        raise DeployError(f'{what}失败（退出码 {result.returncode}）；请检查权限和网络')
    return result


def valid_value(value, label):
    if any(char in value for char in ('\n', '\r', '\0')):
        raise DeployError(f'{label}不能包含换行或 NUL')
    return value


def validate_store(value):
    valid_value(value, 'store')
    parsed = urllib.parse.urlsplit(value)
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise DeployError('store 不支持查询参数、片段或内嵌凭据')
    if parsed.scheme == 's3' and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', parsed.netloc):
        if parsed.netloc.lower() in ('your_bucket', 'your-bucket', 'other_bucket', 'other-bucket'):
            raise DeployError('store 仍是模板占位符，请填写实际 S3 任务通道')
        return value.rstrip('/')
    if parsed.scheme == 'file' and not parsed.netloc and parsed.path.startswith('/'):
        # Store treats the URI literally; percent-escaped paths would silently select another directory.
        if '%' in parsed.path:
            raise DeployError('file:// 路径请直接填写本机绝对路径，不要进行 URL 编码')
        return value.rstrip('/') if parsed.path != '/' else value
    raise DeployError('store 必须是 s3://桶/任务前缀 或 file:///绝对路径')


def validate_endpoint(value):
    valid_value(value, 'endpoint')
    if value:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise DeployError('endpoint 必须是 HTTP(S) 地址，且不能内嵌凭据或查询参数')
    return value


def load_ini(path):
    parser = configparser.ConfigParser(interpolation=None)
    if path.is_symlink():
        raise DeployError(f'配置或状态文件是符号链接，未修改：{path}')
    if path.exists():
        try:
            with path.open(encoding='utf-8') as stream:
                parser.read_file(stream)
        except (OSError, UnicodeError, configparser.Error):
            raise DeployError(f'配置或状态文件无法解析，已保留：{path}') from None
    return parser


def atomic_text(path, text, *, backup=False):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise DeployError(f'目标不是普通文件，未修改：{path}')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and path.read_text(encoding='utf-8') == text:
        return None
    previous = None
    if backup and path.exists():
        suffix = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
        previous = path.with_name(path.name + '.backup-' + suffix)
        with previous.open('xb') as stream:
            os.chmod(previous, 0o600)
            stream.write(path.read_bytes())
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    return previous


def update_config(path, updates):
    """Replace only requested [client] keys; retain other sections and comments verbatim."""
    load_ini(path)
    text = path.read_text(encoding='utf-8') if path.exists() else ''
    lines = text.splitlines(keepends=True)
    output, pending, in_client, skipping_value = [], dict(updates), False, False
    found_client = False
    for line in lines:
        section = re.match(r'^\s*\[([^\]]+)\]', line)
        if section:
            if in_client:
                output.extend(f'{key} = {value}\n' for key, value in pending.items())
                pending.clear()
            in_client = section.group(1) == 'client'
            found_client |= in_client
            skipping_value = False
        key_match = re.match(r'^\s*([^#;\s][^=:]*?)\s*[=:]', line) if in_client and not section else None
        if key_match and key_match.group(1).strip().lower() in updates:
            key = key_match.group(1).strip().lower()
            output.append(f'{key} = {updates[key]}\n')
            pending.pop(key, None)
            skipping_value = True
            continue
        if skipping_value and line[:1].isspace() and line.strip() and not line.lstrip().startswith(('#', ';')) and not key_match:
            continue
        if key_match or section or (line.strip() and not line.lstrip().startswith(('#', ';'))):
            skipping_value = False
        output.append(line)
    if not found_client:
        if output and not output[-1].endswith('\n'):
            output[-1] += '\n'
        output.append('\n[client]\n')
    if pending:
        if output and not output[-1].endswith('\n'):
            output[-1] += '\n'
        output.extend(f'{key} = {value}\n' for key, value in pending.items())
    new_text = ''.join(output)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(new_text)
    except configparser.Error:
        raise DeployError('更新后的配置无效，原配置已保留') from None
    return atomic_text(path, new_text, backup=True)


def write_state(path, values):
    text = '[deployment]\n' + ''.join(f'{key} = {valid_value(str(value), key)}\n' for key, value in values.items())
    atomic_text(path, text)


def fetch_package(source, destination, expected_hash, *, aws=None, env=None):
    if expected_hash and not re.fullmatch(r'[0-9a-fA-F]{64}', expected_hash):
        raise DeployError('--sha256 必须是 64 位十六进制 SHA256')
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme in ('https', 's3') and not expected_hash:
        raise DeployError('自定义在线安装包必须同时指定 --sha256；本地包会验证内部 MANIFEST')
    if parsed.scheme == 'https':
        if parsed.username or parsed.password:
            raise DeployError('下载地址不能内嵌凭据')
        try:
            request = urllib.request.Request(source, headers={'User-Agent': 'awscli-agent-deploy'})
            with urllib.request.urlopen(request, timeout=30) as response, destination.open('wb') as target:
                if urllib.parse.urlsplit(response.geturl()).scheme != 'https':
                    raise DeployError('下载重定向到了非 HTTPS 地址')
                total = 0
                deadline = time.monotonic() + 120
                while chunk := response.read(1024 * 1024):
                    if time.monotonic() > deadline:
                        raise DeployError('安装包下载总时长超过 120 秒')
                    total += len(chunk)
                    if total > MAX_PACKAGE_BYTES:
                        raise DeployError('安装包超过 64 MiB，已停止下载')
                    target.write(chunk)
        except DeployError:
            raise
        except Exception as error:
            raise DeployError(f'安装包下载失败（{type(error).__name__}），请检查网络或使用 --package 本地包') from None
    elif parsed.scheme == 's3':
        if not aws:
            raise DeployError('下载 S3 安装包需要官方 AWS CLI')
        checked_run([aws, 's3', 'cp', source, str(destination), '--only-show-errors', '--cli-connect-timeout', '10', '--cli-read-timeout', '30'],
                    env=env, timeout=120, what='S3 安装包下载')
    elif not parsed.scheme:
        original = Path(source).expanduser()
        if not original.is_file() or original.stat().st_size > MAX_PACKAGE_BYTES:
            raise DeployError('本地安装包不存在或超过 64 MiB')
        shutil.copyfile(original, destination)
    else:
        raise DeployError('--package 支持本地路径、HTTPS 地址或 s3:// 地址')
    if destination.stat().st_size > MAX_PACKAGE_BYTES:
        raise DeployError('安装包超过 64 MiB')
    actual = hashlib.sha256(destination.read_bytes()).hexdigest()
    if expected_hash and actual != expected_hash.lower():
        raise DeployError('安装包 SHA256 校验失败，尚未修改现有安装')
    return actual


def extract_package(archive, directory):
    """Manually extract exactly nine regular package files, never links or tar paths."""
    try:
        with tarfile.open(archive, 'r:gz') as bundle:
            members = bundle.getmembers()
            regular, root, total = {}, None, 0
            for member in members:
                parts = (member.name.rstrip('/') if member.isdir() else member.name).split('/')
                if not parts or parts[0] in ('', '.', '..') or any(part in ('', '.', '..') for part in parts):
                    raise DeployError('安装包含不安全路径')
                if root is None:
                    root = parts[0]
                if parts[0] != root or not re.fullmatch(r'awscli-agent(?:-\d[\w.]*)?', root):
                    raise DeployError('安装包根目录必须是 awscli-agent')
                if member.isdir() and len(parts) == 1:
                    continue
                if len(parts) != 2 or not member.isfile() or parts[1] not in PACKAGE_FILES | {'MANIFEST.sha256'} or parts[1] in regular:
                    raise DeployError('安装包包含非白名单成员、重复成员或链接')
                total += member.size
                if total > MAX_PACKAGE_BYTES or member.size < 0:
                    raise DeployError('安装包解压大小超过 64 MiB')
                regular[parts[1]] = member
            if set(regular) != PACKAGE_FILES | {'MANIFEST.sha256'}:
                raise DeployError('安装包缺少必需文件')
            destination = directory / root
            destination.mkdir(mode=0o700)
            for name, member in regular.items():
                stream = bundle.extractfile(member)
                if stream is None:
                    raise DeployError('安装包成员无法读取')
                with stream, (destination / name).open('xb') as target:
                    shutil.copyfileobj(stream, target)
                (destination / name).chmod(0o600)
    except (tarfile.TarError, OSError, UnicodeError):
        raise DeployError('安装包无法读取，尚未修改现有安装') from None
    try:
        lines = (destination / 'MANIFEST.sha256').read_text(encoding='utf-8').splitlines()
        manifest = dict(line.split('  ', 1)[::-1] for line in lines)
        if len(lines) != len(PACKAGE_FILES) or set(manifest) != PACKAGE_FILES:
            raise ValueError()
        for name, digest in manifest.items():
            if not re.fullmatch(r'[0-9a-f]{64}', digest) or hashlib.sha256((destination / name).read_bytes()).hexdigest() != digest:
                raise ValueError()
    except (OSError, UnicodeError, ValueError):
        raise DeployError('安装包 MANIFEST 校验失败，尚未修改现有安装') from None
    checked_run([sys.executable, str(destination / 'install_agent.py'), '--verify-only'], what='安装器内部校验')
    return destination


def load_package_module(package, name):
    spec = importlib.util.spec_from_file_location('_awscli_deploy_' + name, package / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_install_targets(package, home):
    """Mirror installer ownership checks before stopping an existing healthy process."""
    installer = load_package_module(package, 'install_agent')
    destination = home / '.local/share/awscli-agent'
    marker = destination / installer.MARKER
    if destination.exists():
        if destination.is_symlink() or not marker.is_file() or marker.is_symlink() or marker.read_text() != 'awscli-agent\n':
            raise DeployError('已有 Agent 安装目录不是受管目录，未停止或覆盖')
        for name in (*installer.INSTALLED_FILES, installer.MARKER):
            path = destination / name
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise DeployError('已有 Agent 安装成员不是受管普通文件，未停止或覆盖')
    command = home / '.local/bin/awscli'
    if (command.exists() or command.is_symlink()) and not installer.full_launcher(command):
        if command.is_symlink() or installer.launcher_target(command) != destination / 'agent_entry.py' or not marker.is_file():
            raise DeployError('已有 awscli 不是受管命令，未停止或覆盖')


def probe_store(package, store, profile, endpoint):
    """Exercise transport permissions and conditional writes without submitting a task."""
    module = load_package_module(package, 's3_agent')
    transport = module.Store(store, profile or None, endpoint or None)
    key = '_deploy_probe/' + uuid.uuid4().hex + '/probe'
    payload = b'awscli deployment transport probe\n'
    created, failure = False, None
    try:
        transport.put(key, payload, exclusive=True)
        created = True
        if transport.get(key) != payload or key not in [item['key'] for item in transport.objects(key.rsplit('/', 1)[0] + '/')]:
            raise DeployError('任务通道读写或列表校验不一致')
        try:
            transport.put(key, b'duplicate', exclusive=True)
        except FileExistsError:
            pass
        else:
            raise DeployError('任务通道不支持条件写入，不能安全领取任务')
    except Exception as error:
        failure = error if isinstance(error, DeployError) else DeployError('任务通道探测失败；请检查 S3 凭据、路径和读写/列出/删除权限')
    finally:
        if created:
            try:
                transport.delete(key)
            except Exception:
                failure = DeployError('任务通道临时探测对象清理失败；请检查删除权限')
    if failure:
        raise failure from None


def work_directory(home):
    current, legacy = home / '.local/state/awscli-agent', home / '.local/state/s3a-agent'
    return legacy if legacy.is_dir() and not current.exists() else current


def owner_value(work):
    path = work / 'owner.json'
    if not path.exists():
        return ''
    try:
        value = json.loads(path.read_text(encoding='utf-8'))['owner']
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
            raise ValueError()
        return value
    except (OSError, ValueError, KeyError, TypeError):
        raise DeployError('现有 owner.json 无效，已保留执行状态；请先排查') from None


def process_identity(pid):
    try:
        os.kill(pid, 0)
        proc = Path('/proc') / str(pid)
        if proc.exists():
            stat = (proc / 'stat').read_text()
            start = stat.rsplit(')', 1)[1].split()[19]
            command = (proc / 'cmdline').read_bytes()
        else:
            start = checked_run(['ps', '-p', str(pid), '-o', 'lstart='], timeout=5, what='进程身份探测').stdout.decode().strip()
            command = checked_run(['ps', '-ww', '-p', str(pid), '-o', 'command='], timeout=5, what='进程命令探测').stdout.strip()
        if not start or not command:
            return None
        return start, hashlib.sha256(command).hexdigest(), command
    except (OSError, DeployError, IndexError):
        return None


def user_systemd_available():
    if not shutil.which('systemctl'):
        return False
    try:
        result = subprocess.run(['systemctl', '--user', 'is-system-running'], capture_output=True, timeout=5)
        return result.stdout.strip() in (b'running', b'degraded')
    except (OSError, subprocess.TimeoutExpired):
        return False


def service_active(unit=SERVICE_NAME):
    try:
        return subprocess.run(['systemctl', '--user', 'is-active', '--quiet', unit], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def managed_service(path):
    return path.is_file() and not path.is_symlink() and path.read_text(encoding='utf-8').startswith(SERVICE_MARKER)


def controlled_pid(state, service, work):
    """Validate the process we may later stop, without interrupting a working Agent."""
    if service_active('s3a-agent.service'):
        raise DeployError('旧 s3a-agent 用户服务仍在运行，请先 systemctl --user stop s3a-agent，再重新部署')
    if service_active():
        if not managed_service(service) or state.get('managed_by') != MANAGED or state.get('mode') != 'service':
            raise DeployError('已有非部署脚本管理的 awscli-agent 服务，请先手动停止，未覆盖运行代码')
        result = checked_run(['systemctl', '--user', 'show', SERVICE_NAME, '--property=MainPID', '--value'], timeout=5, what='验证已有受管服务 PID')
        try:
            pid = int(result.stdout.strip())
        except ValueError:
            raise DeployError('已有受管服务 PID 无效，未操作进程') from None
        identity = process_identity(pid) if pid > 1 else None
        if identity is None or (state.get('command_sha256') and identity[1] != state['command_sha256']) or state.get('owner', '') != owner_value(work):
            raise DeployError('已有受管服务的进程命令或 owner 不匹配，未停止或覆盖')
        return pid
    if state.get('managed_by') != MANAGED or state.get('mode') != 'background':
        return None
    try:
        pid = int(state.get('pid', '0'))
    except ValueError:
        raise DeployError('旧部署 PID 状态无效，未操作任何进程') from None
    if pid <= 1:
        return None
    identity = process_identity(pid)
    if identity is None:
        return None
    if (identity[0] != state.get('process_start') or identity[1] != state.get('command_sha256')
            or str(Path.home() / '.local/share/awscli-agent/agent_entry.py').encode() not in identity[2]
            or state.get('owner', '') != owner_value(work)):
        raise DeployError('旧部署 PID 仍存活但身份或 owner 不匹配，未终止未知进程')
    return pid


def stop_managed(state, service, work):
    pid = controlled_pid(state, service, work)
    if state.get('managed_by') == MANAGED and state.get('mode') == 'service' and managed_service(service):
        if pid is not None:
            checked_run(['systemctl', '--user', 'disable', '--now', SERVICE_NAME], timeout=30, what='停止已有受管 Agent 服务')
        elif user_systemd_available():
            checked_run(['systemctl', '--user', 'disable', SERVICE_NAME], timeout=15, what='停用已有受管 Agent 服务')
        return
    if pid is None:
        return
    identity = process_identity(pid)
    if identity is None:
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        current = process_identity(pid)
        if current is None or current[:2] != identity[:2]:
            return
        time.sleep(0.1)
    raise DeployError('已有 Agent 停止超时；未强杀进程或覆盖运行代码，请手动排查后重试')


def reject_other_agents(name, skip_pid=None):
    """Catch a manually started same-name process even if it uses a different work directory."""
    proc = Path('/proc')
    if not proc.is_dir():
        return  # Work-directory flock remains authoritative on platforms without /proc.
    for directory in proc.iterdir():
        if not directory.name.isdigit() or int(directory.name) in (os.getpid(), skip_pid):
            continue
        try:
            if directory.stat().st_uid != os.getuid():
                continue
            args = (directory / 'cmdline').read_bytes().split(b'\0')
            tokens = [arg.decode(errors='replace') for arg in args if arg]
            if not any(Path(arg).name in ('agent_entry.py', 's3_agent.py') for arg in tokens) or 'agent' not in tokens:
                continue
            found = any(arg == '--name=' + name or (arg == '--name' and i + 1 < len(tokens) and tokens[i + 1] == name) for i, arg in enumerate(tokens))
            if found:
                raise DeployError('已有手动运行的同名 Agent，请先停止后重新部署；未操作未知进程')
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue


def acquire_lock(path, message):
    if path.is_symlink():
        raise DeployError(f'锁文件是符号链接，未操作：{path}')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stream = path.open('a')
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        raise LockBusy(message) from None
    return stream


def service_quote(value):
    # systemd quotes and specifiers are not shell quoting. Literal % must be escaped.
    valid_value(value, '服务参数')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def agent_command(home, config, work, settings, name):
    command = [sys.executable, str(home / '.local/share/awscli-agent/agent_entry.py'), '--config', str(config), '--store', settings['store']]
    for option in ('profile', 'endpoint'):
        if settings.get(option):
            command += ['--' + option, settings[option]]
    return command + ['agent', '--name', name, '--work-dir', str(work), '--retention-days', '7']


def start_service(service, command, env, region):
    if service.exists() and not managed_service(service):
        raise DeployError('已有自定义 awscli-agent 用户服务，未覆盖；请选择 --start background 或手动迁移服务')
    # Copy only credential-provider selectors; never persist access keys or session tokens.
    safe_env = {key: env[key] for key in ('PATH', 'AWS_SHARED_CREDENTIALS_FILE', 'AWS_CONFIG_FILE') if env.get(key)}
    if region:
        safe_env['AWS_DEFAULT_REGION'] = region
    text = SERVICE_MARKER + '[Unit]\nDescription=awscli S3 execution agent\nAfter=network-online.target\n\n[Service]\nType=simple\n'
    # ExecStart still expands dollar variables inside quotes; $$ preserves a literal dollar.
    text += 'ExecStart=' + ' '.join(service_quote(arg).replace('$', '$$') for arg in command) + '\n'
    text += ''.join('Environment=' + service_quote(key + '=' + value) + '\n' for key, value in safe_env.items())
    # Global command options are authoritative; ambient manager values must not switch channels.
    text += 'UnsetEnvironment=S3_AGENT_STORE AWS_PROFILE AWS_ENDPOINT_URL\n'
    text += 'Restart=on-failure\nRestartSec=5\nKillMode=control-group\nTimeoutStopSec=20\nUMask=0077\n\n[Install]\nWantedBy=default.target\n'
    atomic_text(service, text, backup=True)
    checked_run(['systemctl', '--user', 'daemon-reload'], timeout=15, what='重载用户服务')
    checked_run(['systemctl', '--user', 'enable', '--now', SERVICE_NAME], timeout=30, what='启动 Agent 用户服务')
    result = checked_run(['systemctl', '--user', 'show', SERVICE_NAME, '--property=MainPID', '--value'], timeout=5, what='读取 Agent 服务 PID')
    try:
        return int(result.stdout.strip())
    except ValueError:
        raise DeployError('Agent 服务未返回有效 PID') from None


def wait_started(pid, work, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_identity(pid) is None:
            raise DeployError('Agent 启动后立即退出，请查看工作目录内的 agent.log')
        try:
            lock = acquire_lock(work / 'agent.lock', '')
        except LockBusy:
            if owner_value(work):
                time.sleep(0.3)
                if process_identity(pid) is None:
                    raise DeployError('Agent 启动后立即退出，请查看工作目录内的 agent.log')
                return
        else:
            lock.close()
        time.sleep(0.1)
    raise DeployError('Agent 未取得工作目录锁，启动验证超时；请查看 agent.log')


def configure_logger(path):
    cutoff = time.time() - 7 * 86400
    for candidate in (path, path.with_name(path.name + '.1'), path.with_name(path.name + '.2')):
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
            raise DeployError('部署日志目标不是普通文件，未跟随链接或修改')
        if candidate.exists() and candidate.stat().st_mtime < cutoff:
            candidate.unlink()
    logger = logging.getLogger('awscli.deploy')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=2, encoding='utf-8')
    path.chmod(0o600)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
    return logger


def close_logger(logger):
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)


def _main(argv, resources):
    parser = argparse.ArgumentParser(description='通用 awscli Agent 一键部署；保留配置、owner 和执行记录，不保存 AWS 密钥。')
    parser.add_argument('--name', default='server-a', help='Agent 名称，客户端必须使用相同名称')
    parser.add_argument('--store', help='S3 任务通道，如 s3://BUCKET/awscli-channel；也支持 file:///绝对路径')
    parser.add_argument('--profile', help='使用服务器已配置的 AWS profile；空值清除已有 profile')
    parser.add_argument('--endpoint', help='S3 兼容服务 HTTP(S) 地址；空值清除已有 endpoint')
    parser.add_argument('--region', help='AWS 默认区域（保存在部署状态，服务运行时设置）')
    parser.add_argument('--work-dir', help='沿用旧 Agent 的持久化工作目录；升级不能更换已有受管任务目录')
    parser.add_argument('--package', default=PACKAGE_URL, help='本地 tar.gz、HTTPS 或 S3 安装包；默认已核对的 GitHub 0.4.1 Agent 包')
    parser.add_argument('--sha256', help='安装包预期 SHA256；自定义在线安装包必须指定')
    parser.add_argument('--no-start', action='store_true', help='仅安装和配置，不启动（仍会验证任务通道）')
    parser.add_argument('--start', choices=('auto', 'foreground', 'background', 'service'), default='auto', help='默认优先可用的用户 systemd，否则后台；容器可使用 background')
    parser.add_argument('--no-deps', action='store_true', help='不自动安装缺失的依赖')
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):
        raise DeployError('需要 Python 3.10+，请通过 deploy.sh 启动')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', args.name):
        raise DeployError('--name 必须是有效的 Agent 标识符')
    for field in ('profile', 'region'):
        if getattr(args, field) is not None:
            valid_value(getattr(args, field), field)
    home = Path.home()
    config = home / '.config/s3-agent/config.ini'
    deploy_dir = home / '.local/state/awscli-deploy'
    deploy_lock = acquire_lock(deploy_dir / 'deploy.lock', '另一个部署进程正在运行，请等待其完成')
    resources.callback(deploy_lock.close)
    logger = configure_logger(deploy_dir / 'deploy.log')
    resources.callback(close_logger, logger)
    state_path = deploy_dir / 'deployment.ini'
    state_ini = load_ini(state_path)
    state = dict(state_ini['deployment']) if state_ini.has_section('deployment') else {}
    prior_work = Path(state['work_dir']).expanduser().resolve() if state.get('managed_by') == MANAGED and state.get('work_dir') else None
    if args.work_dir is not None:
        valid_value(args.work_dir, 'work-dir')
        work = Path(args.work_dir).expanduser().resolve()
        if prior_work is not None and work != prior_work and (prior_work / 'owner.json').exists():
            raise DeployError('已有受管工作目录包含 owner 和任务状态，未更换目录；请继续使用原 --work-dir')
    else:
        work = prior_work or work_directory(home)
    existing = load_ini(config)
    client = dict(existing['client']) if existing.has_section('client') else {}
    store = args.store if args.store is not None else client.get('store') or os.environ.get('S3_AGENT_STORE')
    try:
        store = validate_store(store or '')
    except DeployError:
        if args.store is None and sys.stdin.isatty():
            store = validate_store(input('请输入 S3 任务通道（如 s3://桶名/awscli-channel）：').strip())
        else:
            raise DeployError('未配置有效任务通道；请重新运行：bash deploy.sh --store s3://桶名/awscli-channel --name ' + args.name) from None
    settings = {'store': store}
    for field, env_name in (('profile', 'AWS_PROFILE'), ('endpoint', 'AWS_ENDPOINT_URL')):
        explicit = getattr(args, field)
        settings[field] = explicit if explicit is not None else client.get(field, os.environ.get(env_name, ''))
        valid_value(settings[field], field)
    validate_endpoint(settings['endpoint'])
    region = args.region if args.region is not None else state.get('region') or os.environ.get('AWS_DEFAULT_REGION') or os.environ.get('AWS_REGION', '')
    valid_value(region, 'region')
    service = home / '.config/systemd/user' / SERVICE_NAME
    mode = ('service' if user_systemd_available() else 'background') if args.start == 'auto' else args.start
    if mode == 'service' and not args.no_start and not user_systemd_available():
        raise DeployError('用户 systemd 不可用，请使用 --start background（适用于容器）')
    if state.get('managed_by') == MANAGED and state.get('name') and state['name'] != args.name and (work / 'owner.json').exists():
        raise DeployError('已有部署使用不同 Agent 名称；为保留其任务状态，未在同一工作目录更名，请继续使用原 --name')
    env = dict(os.environ)
    env['PATH'] = str(home / '.local/bin') + os.pathsep + env.get('PATH', os.defpath)
    env['AWS_PAGER'] = ''
    for key in ('S3_AGENT_STORE', 'AWS_PROFILE', 'AWS_ENDPOINT_URL'):
        env.pop(key, None)
    if settings['profile']:
        env['AWS_PROFILE'] = settings['profile']
    if settings['endpoint']:
        env['AWS_ENDPOINT_URL'] = settings['endpoint']
    if region:
        env['AWS_DEFAULT_REGION'] = region
    if mode == 'service' and not args.no_start:
        if service.exists() and not managed_service(service):
            raise DeployError('已有自定义 awscli-agent 用户服务，未覆盖；请选择 --start background 或手动迁移服务')
        if not settings['profile'] and any(env.get(key) for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN')):
            if args.start == 'auto':
                mode = 'background'
            else:
                raise DeployError('当前凭据存在于终端环境；用户服务不会保存密钥，请配置 AWS profile/IAM Role 或使用 --start background')

    def step(message):
        print(message, flush=True)
        logger.info(message)

    running_lock = None
    try:
        step('检查已有 Agent 和部署状态…')
        # Check ownership now, but keep a working Agent running throughout network/package preflight.
        known_pid = controlled_pid(state, service, work)
        reject_other_agents(args.name, skip_pid=known_pid)
        try:
            inspection_lock = acquire_lock(work / 'agent.lock', '已有手动启动的 Agent 占用工作目录，请先停止，未覆盖运行代码')
        except LockBusy:
            if known_pid is None:
                raise
        else:
            inspection_lock.close()
        previous_owner = owner_value(work)
        needs_aws = store.startswith('s3://') or args.package.startswith('s3://')
        aws = None
        if needs_aws:
            step('检查官方 AWS CLI 及 S3 条件写入支持…')
            from deploy_dependencies import ensure_aws
            aws = str(ensure_aws(no_deps=args.no_deps))
            env['PATH'] = str(Path(aws).parent) + os.pathsep + env['PATH']
        # Package Store launches aws through os.environ; retain provider environment only for the bounded probe.
        with tempfile.TemporaryDirectory(prefix='awscli-deploy-') as temporary:
            directory = Path(temporary)
            archive = directory / 'awscli-agent.tar.gz'
            expected = args.sha256 or (PACKAGE_SHA256 if args.package == PACKAGE_URL else None)
            step('下载并校验 Agent 安装包…')
            digest = fetch_package(args.package, archive, expected, aws=aws, env=env)
            package = extract_package(archive, directory)
            validate_install_targets(package, home)
            version_match = re.search(r'^VERSION\s*=\s*[\'"]([^\'"]+)', (package / 's3_agent.py').read_text(), re.MULTILINE)
            if version_match is None:
                raise DeployError('安装包未提供 VERSION，尚未修改现有安装')
            version = version_match.group(1)
            valid_value(version, 'VERSION')
            step('检查任务通道读写、列表、删除及条件写入…')
            previous_environment = dict(os.environ)
            try:
                os.environ.clear()
                os.environ.update(env)
                probe_store(package, store, settings['profile'], settings['endpoint'])
            finally:
                os.environ.clear()
                os.environ.update(previous_environment)
            if not args.no_start:
                shared = load_package_module(package, 's3_agent')
                if config.exists():
                    try:
                        parsed_config = shared.read_config(config)
                        servers = parsed_config.get('servers', {})
                        for alias, server in servers.items():
                            shared.identifier(alias)
                            shared.identifier(server.get('agent', alias))
                            if set(server) - {'agent', 'store', 'profile', 'endpoint', 'git_repo'}:
                                raise ValueError()
                        if parsed_config.get('default_server') and parsed_config['default_server'] not in servers:
                            raise ValueError()
                    except Exception:
                        raise DeployError('已有配置包含 Agent 不支持的字段或分组；配置已保留，请修正后重试或使用 --no-start') from None
            # Recheck immediately before writes: a manual Agent may have started during downloads.
            stop_managed(state, service, work)
            reject_other_agents(args.name)
            running_lock = acquire_lock(work / 'agent.lock', '已有手动启动的 Agent 占用工作目录，请先停止，未覆盖运行代码')
            step('安装 Agent，保留已有配置和执行状态…')
            result = checked_run([sys.executable, str(package / 'install_agent.py')], env=env, timeout=30, what='Agent 安装')
            # Installer prints paths and migration details only; avoid relaying arbitrary package output.
            if result.returncode:
                raise DeployError('Agent 安装失败')
            updates = {field: getattr(args, field) for field in ('store', 'profile', 'endpoint') if getattr(args, field) is not None}
            if not client.get('store') or client.get('store') != store and args.store is None:
                updates['store'] = store
            backup = update_config(config, updates)
            if backup:
                step(f'已有配置已备份：{backup}')
            if previous_owner and owner_value(work) != previous_owner:
                raise DeployError('owner 在部署期间意外变化，未启动 Agent，请先排查状态目录')
        values = {'managed_by': MANAGED, 'name': args.name, 'mode': 'installed', 'status': 'installed',
                  'version': version, 'package_sha256': digest, 'work_dir': str(work), 'config': str(config),
                  'region': region, 'owner': owner_value(work), 'updated_at': dt.datetime.now(dt.timezone.utc).isoformat()}
        write_state(state_path, values)
        step(f'Agent {version} 已安装；配置：{config}')
        if args.no_start:
            step('已按 --no-start 完成安装，Agent 未启动。')
            return 0
        command = agent_command(home, config, work, settings, args.name)
        running_lock.close()
        running_lock = None
        step('启动 Agent（' + mode + '）…')
        child = None
        service_attempt = False
        try:
            if mode == 'service':
                # Services cannot inherit terminal-only credential variables. Existing profiles/IAM role are required.
                if not settings['profile'] and (env.get('AWS_ACCESS_KEY_ID') or env.get('AWS_SECRET_ACCESS_KEY') or env.get('AWS_SESSION_TOKEN')):
                    raise DeployError('当前凭据仅存在于终端环境；用户服务不会保存密钥，请配置 AWS profile/IAM Role 或使用 --start background')
                service_attempt = True
                pid = start_service(service, command, env, region)
            else:
                output = None if mode == 'foreground' else subprocess.DEVNULL
                child = subprocess.Popen(command, env=env, stdin=None if mode == 'foreground' else subprocess.DEVNULL,
                                         stdout=output, stderr=output, start_new_session=mode != 'foreground')
                pid = child.pid
            wait_started(pid, work)
            identity = process_identity(pid)
            if identity is None:
                raise DeployError('Agent 启动验证失败')
        except BaseException:
            cleanup_ok = True
            try:
                if child is not None and child.poll() is None:
                    child.terminate()
                    child.wait(timeout=20)
                elif service_attempt and managed_service(service):
                    checked_run(['systemctl', '--user', 'disable', '--now', SERVICE_NAME], timeout=30, what='停止本次启动失败的受管服务')
            except Exception:
                cleanup_ok = False
            values.update(status='start_failed', mode=mode, pid=0, owner=owner_value(work))
            if child is not None and child.poll() is None:
                remaining_identity = process_identity(child.pid)
                if remaining_identity:
                    values.update(pid=child.pid, process_start=remaining_identity[0], command_sha256=remaining_identity[1])
            write_state(state_path, values)
            step('本次启动失败，新增 Agent 已停止；安装和执行状态已保留。' if cleanup_ok else '本次启动失败，停止新 Agent 尚未完成；请检查部署状态中的 PID 和用户服务。')
            raise
        values.update(mode=mode, status='running', pid=pid, process_start=identity[0], command_sha256=identity[1], owner=owner_value(work))
        write_state(state_path, values)
        step(f'Agent 正在运行；PID：{pid}；名称：{args.name}')
        step(f'执行日志：{work / "agent.log"}；部署日志：{deploy_dir / "deploy.log"}')
        step(f'部署状态：{state_path}；客户端需使用相同的 store 和 Agent 名称。')
        if mode == 'foreground':
            try:
                return child.wait()
            except KeyboardInterrupt:
                with contextlib.suppress(ProcessLookupError):
                    child.send_signal(signal.SIGTERM)
                child.wait(timeout=20)
                values.update(status='stopped', pid=0)
                write_state(state_path, values)
                return 130
        return 0
    except Exception as error:
        logger.error('deployment_failed type=%s', type(error).__name__)
        raise
    finally:
        if running_lock:
            running_lock.close()
        deploy_lock.close()
        close_logger(logger)


def main(argv=None):
    # Setup errors (invalid configuration, missing store, log symlink) must also release resources.
    with contextlib.ExitStack() as resources:
        return _main(argv, resources)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (DeployError, KeyboardInterrupt) as error:
        print('部署失败：' + (str(error) if isinstance(error, DeployError) else '已取消'), file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print('部署失败：' + type(error).__name__ + '；未输出可能包含凭据的异常详情', file=sys.stderr)
        sys.exit(1)
