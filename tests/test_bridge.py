import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ilink_codex.bridge import Bridge
from ilink_codex.config import Allowlist, Config, write_private_json
from ilink_codex.runner import Result
from ilink_codex.store import Store
from ilink_codex.transport import ILinkTransport, TransportError
from ilink_codex.worker import Worker


def message(mid=1, user='owner', text='实现一个函数', context='ctx'):
    return {'message_id': mid, 'from_user_id': user, 'context_token': context,
            'message_type': 1, 'message_state': 2,
            'item_list': [{'type': 1, 'text_item': {'text': text}}]}


class FakeTransport:
    account = 'bot'

    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, *args):
        self.sent.append(args)
        if self.fail:
            raise RuntimeError('secret server response must not be logged')


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(self.root / 'state', self.root / 'work')
        self.config.state_dir.mkdir()
        self.config.work_dir.mkdir()
        self.acl = Allowlist(self.root / 'allowlist.json')
        write_private_json(self.acl.path, {'bot': ['owner', 'second']})
        self.store = Store(self.root / 'state' / 'db')
        self.transport = FakeTransport()
        self.bridge = Bridge(self.config, self.store, self.acl, self.transport)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def ingest(self, *messages):
        self.bridge.ingest({'msgs': list(messages), 'get_updates_buf': 'cursor-1'})

    def count(self, table):
        return self.store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]

    def test_unknown_users_never_enter_queue_or_receive_reply(self):
        self.ingest(message(user='stranger'))
        self.assertEqual(self.count('tasks'), 0)
        self.assertEqual(self.count('outbox'), 0)
        self.assertEqual(self.count('peers'), 0)
        self.assertEqual(self.store.cursor('bot'), 'cursor-1')

    def test_empty_missing_corrupt_acl_fail_closed(self):
        for value in ('{}', '{"bot": []}', 'broken', '{"bot": "owner"}'):
            self.acl.path.write_text(value)
            self.ingest(message())
            self.assertEqual(self.count('tasks'), 0)
        self.acl.path.unlink()
        self.ingest(message())
        self.assertEqual(self.count('tasks'), 0)

    def test_duplicate_message_is_exactly_one_task_and_receipt(self):
        self.ingest(message(), message())
        self.ingest(message())
        self.assertEqual(self.count('tasks'), 1)
        self.assertEqual(self.count('outbox'), 1)

    def test_batch_and_cursor_rollback_together(self):
        with patch.object(self.store, 'set_cursor', side_effect=RuntimeError('disk error')):
            with self.assertRaises(RuntimeError):
                self.ingest(message())
        self.assertEqual(self.count('tasks'), 0)
        self.assertEqual(self.count('seen'), 0)
        self.assertEqual(self.store.cursor('bot'), '')

    def test_missing_id_context_and_stream_fragments_not_queued(self):
        for msg in (message(mid=None), message(context=''), dict(message(), message_state=1)):
            self.ingest(msg)
        self.assertEqual(self.count('tasks'), 0)

    def test_claim_serializes_each_user_but_allows_other_user(self):
        self.ingest(message(1), message(2), message(3, user='second'))
        first = self.store.claim()
        second = self.store.claim()
        self.assertEqual(first['user'], 'owner')
        self.assertEqual(second['user'], 'second')
        self.assertIsNone(self.store.claim())
        self.store.finish(first, 'succeeded', 'done', 'session-owner')
        self.assertEqual(self.store.claim()['id'], 2)
        self.assertEqual(self.store.peer('bot', 'owner')['session'], 'session-owner')
        self.assertIsNone(self.store.peer('bot', 'second')['session'])

    def test_recovery_interrupts_never_requeues(self):
        self.ingest(message())
        task = self.store.claim()
        self.store.recover()
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.db.execute('SELECT state FROM tasks WHERE id=?', (task['id'],)).fetchone()[0], 'interrupted')
        count = self.count('outbox')
        self.store.recover()
        self.assertEqual(self.count('outbox'), count)

    def test_commands_cancel_and_reset_without_new_tasks(self):
        self.ingest(message(1), message(2))
        task = self.store.claim()
        self.ingest(message(3, text='/new'))
        self.assertEqual(self.count('tasks'), 2)
        self.ingest(message(4, text='/cancel'))
        self.assertTrue(self.store.cancelled(task['id']))
        self.assertIsNone(self.store.claim())
        self.store.finish(task, 'cancelled', 'stopped', 'session-one')
        self.ingest(message(5, text='/new'))
        self.assertIsNone(self.store.peer('bot', 'owner')['session'])

    def test_queue_limit(self):
        for i in range(15):
            self.ingest(message(i))
        self.assertEqual(self.count('tasks'), 10)

    async def test_send_retry_does_not_rerun_and_keeps_client_id(self):
        self.ingest(message())
        self.transport.fail = True
        with self.assertLogs('ilink_codex.bridge') as logs:
            await self.bridge.send_once()
        self.assertNotIn('secret server response', '\n'.join(logs.output))
        self.transport.fail = False
        self.store.db.execute('UPDATE outbox SET next_at=0')
        await self.bridge.send_once()
        self.assertEqual(self.transport.sent[0], self.transport.sent[1])
        self.assertEqual(self.count('tasks'), 1)
        self.assertEqual(self.store.db.execute('SELECT state FROM outbox').fetchone()[0], 'sent')

    async def test_revoke_blocks_pending_execution_and_reply(self):
        self.ingest(message())
        write_private_json(self.acl.path, {'bot': []})

        class NeverRun:
            async def run(self, *args):
                raise AssertionError('revoked user executed')

        worker = Worker(self.config, self.store, self.acl, NeverRun())
        await worker.execute(self.store.claim())
        await self.bridge.send_once()
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(self.store.db.execute('SELECT state FROM tasks').fetchone()[0], 'blocked')

    async def test_result_survives_reopen_and_uses_fresh_context(self):
        self.ingest(message())
        task = self.store.claim()
        self.store.finish(task, 'succeeded', 'saved result', 'session-one')
        self.store.close()
        self.store = Store(self.root / 'state' / 'db')
        self.bridge.store = self.store
        self.ingest(message(2, text='/result', context='fresh-context'))
        while await self.bridge.send_once():
            pass
        self.assertTrue(all(args[2] == 'fresh-context' for args in self.transport.sent))
        self.assertIn('saved result', self.transport.sent[-1][1])
        self.assertEqual(self.store.peer('bot', 'owner')['session'], 'session-one')

    async def test_bridge_worker_reply_flow_and_resume(self):
        sessions = []

        class Runner:
            async def run(self, task, session, cancelled):
                sessions.append(session)
                return Result('succeeded', '任务完成', 'my-session')

        worker = Worker(self.config, self.store, self.acl, Runner())
        for i in (1, 2):
            self.ingest(message(i))
            await worker.execute(self.store.claim())
        while await self.bridge.send_once():
            pass
        self.assertEqual(sessions, [None, 'my-session'])
        self.assertEqual(len(self.transport.sent), 4)
        self.assertTrue(all(r[0] == 'owner' for r in self.transport.sent))

    async def test_worker_global_concurrency_and_per_user_lock(self):
        from dataclasses import replace
        config = replace(self.config, max_concurrency=2)
        self.ingest(message(1), message(2), message(3, user='second'))
        started, release = [], asyncio.Event()

        class Runner:
            async def run(self, task, session, cancelled):
                started.append(task['id'])
                await release.wait()
                return Result('succeeded', 'done')

        worker = Worker(config, self.store, self.acl, Runner())
        service = asyncio.create_task(worker.run())
        try:
            for _ in range(30):
                if len(started) == 2:
                    break
                await asyncio.sleep(.02)
            self.assertEqual(started, [1, 3])
            release.set()
            for _ in range(50):
                if len(started) == 3:
                    break
                await asyncio.sleep(.02)
            self.assertEqual(started, [1, 3, 2])
        finally:
            service.cancel()
            await asyncio.gather(service, return_exceptions=True)

    def test_transport_checks_business_errors(self):
        transport = ILinkTransport({'base_url': 'https://ilinkai.weixin.qq.com', 'bot_token': 'test', 'account_id': 'bot'})
        with patch('weixin_ilink.api.send_message', return_value={'ret': -1}):
            with self.assertRaises(TransportError):
                transport.send('user', 'text', 'ctx', 'fixed-id')
        with patch('weixin_ilink.api.get_updates', return_value={'errcode': -14}):
            with self.assertRaises(TransportError):
                transport.poll('cursor')

    def test_workspace_hash_blocks_path_traversal_and_distinguishes_accounts(self):
        path = self.config.workspace('bot', '../../outside')
        self.assertEqual(path.parent, self.config.work_dir)
        self.assertNotEqual(path, self.config.workspace('another', '../../outside'))

    def test_private_config_mode(self):
        self.assertEqual(self.acl.path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
