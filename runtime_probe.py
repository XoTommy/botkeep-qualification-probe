"""Secrets-free Node 22 / GMGN runtime installation probe."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time
import urllib.request

root = Path('.qualification-runtime')
root.mkdir(exist_ok=True)
manifest = urllib.request.urlopen('https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt', timeout=30).read().decode()
checksum, filename = next(line.split() for line in manifest.splitlines() if line.endswith('-linux-x64.tar.xz'))
archive = root / filename
node_dir = root / filename.removesuffix('.tar.xz')
if not node_dir.exists():
    urllib.request.urlretrieve('https://nodejs.org/dist/latest-v22.x/' + filename, archive)
    assert hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest() == checksum
    with tarfile.open(archive) as source:
        source.extractall(root, filter='data')
    archive.unlink()
runtime_env = os.environ.copy()
runtime_env['PATH'] = str((node_dir / 'bin').resolve()) + os.pathsep + runtime_env.get('PATH', '')
gmgn_root = root / 'gmgn'
subprocess.run([str((node_dir / 'bin' / 'npm').resolve()), 'install', '--prefix', str(gmgn_root),
                '--no-audit', '--no-fund', 'gmgn-cli@1.6.4'], env=runtime_env, check=True, timeout=120)
node = subprocess.check_output([str((node_dir / 'bin' / 'node').resolve()), '--version'], text=True).strip()
gmgn = subprocess.check_output([str((gmgn_root / 'node_modules' / '.bin' / 'gmgn-cli').resolve()), '--version'],
                              env=runtime_env, text=True).strip()
print(json.dumps({'event':'runtime_ready','utc':time.time(),'node':node,'gmgn':gmgn,
                  'official_node_sha256_verified':True,
                  'runtime_file_bytes':sum(p.stat().st_size for p in root.rglob('*') if p.is_file())}), flush=True)
