# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 IT-EXPRESS Bayern and LunarBridge contributors.
"""Download pinned upstream test inputs; verify every file before using it."""
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
manifest = json.loads((ROOT / 'tests/reference-manifest.json').read_text(encoding='utf-8'))
base = (ROOT / 'tests/.references').resolve()
for entry in manifest['files']:
    target = (base / entry['path']).resolve()
    if base not in target.parents:
        raise ValueError('Reference path escapes test directory')
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == entry['sha256']:
        print('VERIFIED cached ' + entry['path'])
        continue
    request = urllib.request.Request(entry['url'], headers={'User-Agent': 'LunarBridge-source-validation/0.1'})
    with urllib.request.urlopen(request, timeout=35) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != entry['sha256']:
        raise ValueError('SHA256 mismatch: ' + entry['path'])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    print('VERIFIED ' + entry['path'])
print('External test references retain their upstream licenses; they are not vendored project source.')
