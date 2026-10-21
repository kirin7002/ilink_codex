"""Explicit local import from stdin into the dedicated Docker Codex volume."""
import json
import os
import sys
from pathlib import Path

os.umask(0o077)
data = json.load(sys.stdin)
if not isinstance(data, dict) or not (data.get('tokens') or data.get('OPENAI_API_KEY')):
    raise SystemExit('Not a recognized Codex auth file; nothing was written.')
path = Path.home() / '.codex' / 'auth.json'
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
# Exclusive creation: never replace credentials refreshed by the container.
with path.open('x') as f:
    json.dump(data, f)
print('Codex login imported into the dedicated volume. No credential values displayed.')
