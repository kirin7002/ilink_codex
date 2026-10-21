from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .bridge import Bridge
from .config import Allowlist, Config, write_private_json
from .runner import CodexRunner
from .store import Store
from .transport import ILinkTransport
from .worker import Worker


@contextmanager
def singleton(path: Path):
    with path.open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('This service is already running for this state directory') from None
        yield


async def service(coro):
    task = asyncio.create_task(coro)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass


def login(cfg, replace=False):
    from weixin_ilink.auth import login_with_qr
    import qrcode
    credentials_path = cfg.state_dir / 'creds.json'
    if credentials_path.exists() and not replace:
        raise ValueError('Credentials already exist; use login --replace for explicit re-login')

    def show_qr(value):
        qr = qrcode.QRCode()
        qr.add_data(value)
        qr.print_ascii(invert=True)
        print('请用本人的微信扫码授权；二维码不要转发给他人。')

    credentials = login_with_qr(on_qrcode=show_qr)
    if not credentials.user_id:
        raise ValueError('No owner user ID returned; refusing to initialize authorization')
    acl_path = cfg.state_dir / 'allowlist.json'
    acl = json.loads(acl_path.read_text()) if acl_path.exists() else {}
    users = acl.setdefault(credentials.account_id, [])
    if credentials.user_id not in users:
        users.append(credentials.user_id)
    write_private_json(acl_path, acl)
    write_private_json(credentials_path, asdict(credentials))
    print('登录成功，扫码用户已加入白名单。凭据已保存，未在终端输出。')


def doctor(cfg):
    result = subprocess.run([cfg.codex_bin, '--version'], capture_output=True, text=True)
    print(result.stdout.strip())
    if result.returncode:
        raise RuntimeError('Codex version check failed')
    if result.stdout.strip() != 'codex-cli 0.163.0':
        print('注意：当前版本不是验证过的 0.163.0，请重新验证新建/续接参数。')
    for session in (None, '00000000-0000-0000-0000-000000000000'):
        command = CodexRunner(cfg).command(session)
        command[-1] = '--help'
        check = subprocess.run(command, capture_output=True)
        if check.returncode:
            raise RuntimeError('Codex command argument check failed')
    check = subprocess.run([cfg.codex_bin, 'login', 'status'], capture_output=True)
    print('Codex 登录状态：' + ('可用（仍需真实请求验证）' if check.returncode == 0 else '未就绪'))
    credentials_path = cfg.state_dir / 'creds.json'
    print('iLink 凭据文件：' + ('存在' if credentials_path.exists() else '未配置'))
    print('此检查没有调用模型或微信 API。')


def healthy(cfg, role):
    # A heartbeat alone can remain after a crash. Require the service lock too.
    lock = cfg.state_dir / f'{role}.lock'
    if not lock.exists():
        return False
    with lock.open('r') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            return False
    with sqlite3.connect(f'file:{cfg.state_dir / "tasks.db"}?mode=ro', uri=True, timeout=2) as db:
        row = db.execute('SELECT updated FROM heartbeat WHERE role=?', (role,)).fetchone()
    return bool(row and 0 <= time.time() - row[0] < 120)


def _main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='个人微信 Codex 专线')
    parser.add_argument('--config', type=Path, default=Path('config.toml'))
    commands = parser.add_subparsers(dest='command', required=True)
    login_parser = commands.add_parser('login')
    login_parser.add_argument('--replace', action='store_true')
    commands.add_parser('doctor')
    commands.add_parser('bridge')
    commands.add_parser('worker')
    health = commands.add_parser('health')
    health.add_argument('role', choices=['bridge', 'worker'])
    allow = commands.add_parser('allow', help='本地管理白名单，不提供微信远程授权')
    allow.add_argument('action', choices=['add', 'remove', 'list'])
    allow.add_argument('--user')
    allow.add_argument('--account', help='默认使用当前 bot 账号')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    cfg = Config.load(args.config)
    if args.command == 'health':
        sys.exit(0 if healthy(cfg, args.role) else 1)
    if args.command == 'doctor':
        doctor(cfg)
        return
    if args.command == 'login':
        with singleton(cfg.state_dir / 'admin.lock'):
            login(cfg, args.replace)
        return
    if args.command == 'allow':
        with singleton(cfg.state_dir / 'admin.lock'):
            account = args.account or json.loads((cfg.state_dir / 'creds.json').read_text())['account_id']
            path = cfg.state_dir / 'allowlist.json'
            data = json.loads(path.read_text()) if path.exists() else {}
            users = data.setdefault(account, [])
            if not isinstance(users, list):
                raise ValueError('Invalid allowlist; refusing to overwrite')
            if args.action == 'list':
                print(json.dumps({account: users}, ensure_ascii=False, indent=2))
                return
            if not args.user:
                parser.error('--user is required for add/remove')
            if args.action == 'add' and args.user not in users:
                users.append(args.user)
            elif args.action == 'remove' and args.user in users:
                users.remove(args.user)
            write_private_json(path, data)
            print('白名单已更新；空名单会拒绝所有消息。')
        return
    with singleton(cfg.state_dir / f'{args.command}.lock'):
        store = Store(cfg.state_dir / 'tasks.db', cfg.reply_chunk_chars, cfg.max_reply_chars)
        acl = Allowlist(cfg.state_dir / 'allowlist.json')
        try:
            if args.command == 'bridge':
                creds = json.loads((cfg.state_dir / 'creds.json').read_text())
                app = Bridge(cfg, store, acl, ILinkTransport(creds))
            else:
                app = Worker(cfg, store, acl, CodexRunner(cfg))
            asyncio.run(service(app.run()))
        finally:
            store.close()


def main():
    try:
        _main()
    except Exception as exc:
        # Login/SDK exceptions may contain token-bearing URLs or response bodies.
        logging.error('操作失败 (%s)。请检查配置、凭据状态和网络；原始异常未输出。', type(exc).__name__)
        sys.exit(1)


if __name__ == '__main__':
    main()
