"""Offline functional test using the real queue, runner subprocess, and reply loop."""
import json
import tempfile
import unittest
from pathlib import Path

from ilink_codex.bridge import Bridge
from ilink_codex.config import Allowlist, Config, write_private_json
from ilink_codex.runner import CodexRunner
from ilink_codex.store import Store
from ilink_codex.worker import Worker


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_to_workspace_change_to_weixin_reply_then_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = root / 'codex'
            cli.write_text('''#!/usr/bin/env python3
import sys,json,pathlib
prompt=sys.stdin.read()
resumed='resume' in sys.argv
pathlib.Path('result.txt').write_text('resumed' if resumed else 'created')
print(json.dumps({'type':'thread.started','thread_id':'integration-session'}))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'continued' if resumed else 'created result.txt'}}))
print(json.dumps({'type':'turn.completed'}))
''')
            cli.chmod(0o700)
            config_file = root / 'config.toml'
            config_file.write_text('state_dir="state"\nwork_dir="work"\ncodex_bin=' + json.dumps(str(cli)))
            cfg = Config.load(config_file)
            acl = Allowlist(cfg.state_dir / 'allowlist.json')
            write_private_json(acl.path, {'bot': ['owner']})

            class Transport:
                account = 'bot'
                sent = []
                sequence = 0

                def poll(self, cursor):
                    self.sequence += 1
                    return {'get_updates_buf': str(self.sequence), 'msgs': [{
                        'message_id': self.sequence, 'from_user_id': 'owner', 'context_token': 'context',
                        'message_type': 1, 'message_state': 2,
                        'item_list': [{'type': 1, 'text_item': {'text': 'create a file'}}],
                    }]}

                def send(self, *args):
                    self.sent.append(args)

            transport = Transport()
            for turn in range(2):
                # Reopen state each turn to exercise persistence rather than object memory.
                store = Store(cfg.state_dir / 'tasks.db')
                try:
                    bridge = Bridge(cfg, store, acl, transport)
                    worker = Worker(cfg, store, acl, CodexRunner(cfg))
                    await bridge.poll_once()
                    await bridge.send_once()  # receipt
                    await worker.execute(store.claim())
                    await bridge.send_once()  # final result
                    self.assertEqual(store.peer('bot', 'owner')['session'], 'integration-session')
                finally:
                    store.close()
            self.assertEqual(len(transport.sent), 4)
            self.assertIn('continued', transport.sent[-1][1])
            self.assertEqual((cfg.workspace('bot', 'owner') / 'result.txt').read_text(), 'resumed')
