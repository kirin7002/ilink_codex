"""Exercise the real Codex sandbox without model credentials or external traffic."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

from ilink_codex.config import Config

cfg = Config.load(Path('/app/config.toml'))
fd, outside_name = tempfile.mkstemp(prefix='.sandbox-probe-', dir=cfg.state_dir)
outside = Path(outside_name)
os.close(fd)
outside.write_text('unchanged')
try:
    with tempfile.TemporaryDirectory(prefix='.sandbox-check-', dir=cfg.work_dir) as directory:
        code = f'''
import errno
import socket
from pathlib import Path
Path('inside.txt').write_text('ok')
outside = Path({json.dumps(str(outside))})
try:
    outside.write_text('escaped')
except OSError as exc:
    if exc.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
        raise
    print('OUTSIDE_WRITE_BLOCKED')
else:
    raise SystemExit('ERROR: writing outside workspace was allowed')
try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
except PermissionError:
    print('NETWORK_BLOCKED')
else:
    sock.close()
    raise SystemExit('ERROR: network socket creation was allowed')
print('WORKSPACE_WRITE_OK')
'''
        result = subprocess.run([
            cfg.codex_bin, 'sandbox', '--permission-profile', ':workspace',
            '-C', directory, '--', 'python', '-c', code,
        ], timeout=30, capture_output=True, text=True)
        if result.returncode:
            print(result.stderr[-3000:])
            print(result.stdout[-3000:])
            raise SystemExit('Codex sandbox preflight FAILED; services were not started.')
        assert (Path(directory) / 'inside.txt').read_text() == 'ok'
        assert outside.read_text() == 'unchanged'
        assert all(s in result.stdout for s in ('OUTSIDE_WRITE_BLOCKED', 'NETWORK_BLOCKED', 'WORKSPACE_WRITE_OK'))
        print('Codex sandbox PASS: workspace writes allowed; outside writes and network blocked.')
finally:
    outside.unlink(missing_ok=True)
