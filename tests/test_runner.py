import asyncio
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ilink_codex.config import Config
from ilink_codex.runner import CodexRunner


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake = self.root / 'fake-codex'
        self.config = Config(self.root / 'state', self.root / 'work', codex_bin=str(self.fake), task_timeout=2)
        self.config.work_dir.mkdir()
        self.task = {'id': 1, 'account': 'bot', 'user': 'owner', 'prompt': '请输出 $(touch SHOULD_NOT_EXIST)'}

    def tearDown(self):
        self.temp.cleanup()

    def script(self, body):
        self.fake.write_text('#!/usr/bin/env python3\n' + body)
        self.fake.chmod(0o700)

    async def test_real_subprocess_stdin_json_final_and_resume_arguments(self):
        self.script('''import json,sys,pathlib
text=sys.stdin.read()
pathlib.Path('input.txt').write_text(text)
pathlib.Path('args.json').write_text(json.dumps(sys.argv))
print(json.dumps({'type':'thread.started','thread_id':'session-test'}),flush=True)
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'最终回复'}}),flush=True)
print(json.dumps({'type':'turn.completed'}),flush=True)
''')
        runner = CodexRunner(self.config)
        result = await runner.run(self.task, 'previous-session', lambda: False)
        self.assertEqual(result.state, 'succeeded')
        self.assertEqual(result.text, '最终回复')
        self.assertEqual(result.session, 'session-test')
        work = self.config.workspace('bot', 'owner')
        self.assertIn('$(touch SHOULD_NOT_EXIST)', (work / 'input.txt').read_text())
        self.assertFalse((work / 'SHOULD_NOT_EXIST').exists())
        import json
        args = json.loads((work / 'args.json').read_text())
        self.assertEqual(args[-3:], ['resume', 'previous-session', '-'])
        self.assertIn('approval_policy="never"', args)
        self.assertIn('sandbox_workspace_write.network_access=false', args)

    async def test_nonzero_exit_does_not_report_partial_reply_as_success(self):
        self.script('''import json,sys
sys.stdin.read()
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'partial'}}))
sys.exit(1)
''')
        result = await CodexRunner(self.config).run(self.task, None, lambda: False)
        self.assertEqual(result.state, 'failed')
        self.assertNotEqual(result.text, 'partial')

    async def test_timeout_kills_child_process_group(self):
        self.script('''import subprocess,sys,time,pathlib
sys.stdin.read()
p=subprocess.Popen([sys.executable,'-c',"import time,pathlib; time.sleep(2.5); pathlib.Path('leaked').write_text('bad')"])
pathlib.Path('child.pid').write_text(str(p.pid))
time.sleep(30)
''')
        result = await CodexRunner(replace(self.config, task_timeout=1)).run(self.task, None, lambda: False)
        self.assertEqual(result.state, 'timeout')
        await asyncio.sleep(2)
        self.assertFalse((self.config.workspace('bot', 'owner') / 'leaked').exists())

    async def test_cancellation(self):
        self.script('import sys,time\nsys.stdin.read()\ntime.sleep(30)\n')
        result = await CodexRunner(self.config).run(self.task, None, lambda: True)
        self.assertEqual(result.state, 'cancelled')

    async def test_success_cleans_background_children(self):
        self.script('''import subprocess,sys,json
sys.stdin.read()
subprocess.Popen([sys.executable,'-c',"import time,pathlib; time.sleep(1); pathlib.Path('leaked').write_text('bad')"])
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'done'}}),flush=True)
print(json.dumps({'type':'turn.completed'}),flush=True)
''')
        result = await CodexRunner(self.config).run(self.task, None, lambda: False)
        self.assertEqual(result.state, 'succeeded')
        await asyncio.sleep(1.1)
        self.assertFalse((self.config.workspace('bot', 'owner') / 'leaked').exists())

    async def test_missing_executable(self):
        result = await CodexRunner(self.config).run(self.task, None, lambda: False)
        self.assertEqual(result.state, 'failed')

    async def test_oversized_output_terminates_without_pipe_deadlock(self):
        self.script('''import sys,time
sys.stdin.read()
sys.stdout.write('x' * (2 * 1024 * 1024))
sys.stdout.flush()
time.sleep(30)
''')
        result = await asyncio.wait_for(CodexRunner(self.config).run(self.task, None, lambda: False), 6)
        self.assertEqual(result.state, 'failed')
        self.assertIn('输出超过限制', result.text)


if __name__ == '__main__':
    unittest.main()
