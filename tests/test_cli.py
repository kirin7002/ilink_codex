import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ilink_codex.cli import healthy, login, singleton
from ilink_codex.config import Config
from ilink_codex.store import Store
from weixin_ilink.auth import Credentials


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(self.root / 'state', self.root / 'work')

    def tearDown(self):
        self.temp.cleanup()

    def test_login_binds_only_qr_owner(self):
        credentials = Credentials('test-token', 'bot', 'https://ilinkai.weixin.qq.com', 'qr-owner')
        with patch('weixin_ilink.auth.login_with_qr', return_value=credentials), patch('builtins.print'):
            login(self.config)
        acl = json.loads((self.config.state_dir / 'allowlist.json').read_text())
        self.assertEqual(acl, {'bot': ['qr-owner']})
        self.assertEqual((self.config.state_dir / 'creds.json').stat().st_mode & 0o777, 0o600)

    def test_missing_owner_refuses_to_save_credentials(self):
        credentials = Credentials('test-token', 'bot', 'https://ilinkai.weixin.qq.com')
        with patch('weixin_ilink.auth.login_with_qr', return_value=credentials):
            with self.assertRaises(ValueError):
                login(self.config)
        self.assertFalse((self.config.state_dir / 'creds.json').exists())

    def test_existing_credentials_not_overwritten_implicitly(self):
        self.config.state_dir.mkdir()
        path = self.config.state_dir / 'creds.json'
        path.write_text('original')
        with self.assertRaises(ValueError):
            login(self.config)
        self.assertEqual(path.read_text(), 'original')

    def test_singleton_rejects_second_worker(self):
        with singleton(self.root / 'lock'):
            with self.assertRaises(RuntimeError):
                with singleton(self.root / 'lock'):
                    pass
        with singleton(self.root / 'lock'):
            pass

    def test_health_requires_live_lock_and_recent_heartbeat(self):
        self.config.state_dir.mkdir()
        store = Store(self.config.state_dir / 'tasks.db')
        try:
            self.assertFalse(healthy(self.config, 'worker'))
            store.beat('worker')
            with singleton(self.config.state_dir / 'worker.lock'):
                self.assertTrue(healthy(self.config, 'worker'))
                store.db.execute("UPDATE heartbeat SET updated=0")
                self.assertFalse(healthy(self.config, 'worker'))
                store.beat('worker')
            self.assertFalse(healthy(self.config, 'worker'))
        finally:
            store.close()
