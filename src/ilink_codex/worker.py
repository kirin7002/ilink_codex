from __future__ import annotations

import asyncio
import logging

from .runner import Result

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, config, store, allowlist, runner):
        self.config, self.store, self.allowlist, self.runner = config, store, allowlist, runner

    async def execute(self, task):
        account, user = task['account'], task['user']
        if not self.allowlist.allows(account, user):
            self.store.finish(task, 'blocked', '用户已被移出白名单。')
            return
        try:
            peer = self.store.peer(account, user)
            result = await self.runner.run(task, peer['session'], lambda: (
                self.store.cancelled(task['id']) or not self.allowlist.allows(account, user)))
        except asyncio.CancelledError:
            self.store.finish(task, 'interrupted', '服务停止，任务不会自动重跑；可能已有文件修改。')
            raise
        except Exception as exc:
            log.error('Task %s failed (%s)', task['id'], type(exc).__name__)
            result = Result('failed', '执行器异常；可能已有文件修改，不会自动重跑。')
        self.store.finish(task, result.state, result.text, result.session)

    async def run(self):
        self.store.recover()
        active = set()
        last_heartbeat = 0.0
        try:
            while True:
                now = asyncio.get_running_loop().time()
                if now - last_heartbeat >= 10:
                    self.store.beat('worker')
                    last_heartbeat = now
                done = {task for task in active if task.done()}
                for task in done:
                    task.result()  # Surface storage failures; systemd will restart safely.
                active -= done
                while len(active) < self.config.max_concurrency:
                    task = self.store.claim()
                    if not task:
                        break
                    active.add(asyncio.create_task(self.execute(task)))
                await asyncio.sleep(0.2)
        finally:
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
