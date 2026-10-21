from __future__ import annotations

import asyncio
import logging

from .transport import TransportError

log = logging.getLogger(__name__)


class Bridge:
    def __init__(self, config, store, allowlist, transport):
        self.config, self.store, self.allowlist, self.transport = config, store, allowlist, transport

    def ingest(self, response: dict):
        account = self.transport.account
        with self.store.transaction():
            for msg in response.get('msgs', []):
                if not isinstance(msg, dict):
                    continue
                user = msg.get('from_user_id')
                if not isinstance(user, str) or not self.allowlist.allows(account, user):
                    continue
                self.store.receive(account, msg, self.config)
            cursor = response.get('get_updates_buf')
            if isinstance(cursor, str) and cursor:
                self.store.set_cursor(account, cursor)
            self.store.beat('bridge')

    async def poll_once(self):
        response = await asyncio.to_thread(self.transport.poll, self.store.cursor(self.transport.account))
        self.ingest(response)

    async def send_once(self):
        row = self.store.next_reply(self.transport.account)
        if row is None:
            return False
        if not self.allowlist.allows(row['account'], row['user']):
            self.store.db.execute("UPDATE outbox SET state='blocked' WHERE id=?", (row['id'],))
            return True
        try:
            await asyncio.to_thread(self.transport.send, row['user'], row['body'], row['context'], row['client_id'])
        except Exception as exc:
            # SDK exceptions can embed response bodies and credentials. Never log their text.
            log.warning('Reply %s failed (%s)', row['id'], type(exc).__name__)
            self.store.send_failed(row, self.config.max_send_attempts)
        else:
            self.store.sent(row['id'])
        return True

    async def poll_loop(self):
        failures = 0
        while True:
            try:
                await self.poll_once()
                failures = 0
                await asyncio.sleep(0.2)
            except Exception as exc:
                failures += 1
                log.warning('Poll failed (%s); retrying', type(exc).__name__)
                delay = 3600 if isinstance(exc, TransportError) and exc.code == -14 else min(60, 2 ** min(failures, 6))
                await asyncio.sleep(delay)

    async def send_loop(self):
        while True:
            if not await self.send_once():
                await asyncio.sleep(0.5)

    async def run(self):
        async with asyncio.TaskGroup() as group:
            group.create_task(self.poll_loop())
            group.create_task(self.send_loop())
