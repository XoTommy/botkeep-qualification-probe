"""Fresh synthetic runtime directory; suppressed installer output; no credentials."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

ROOT = Path('.qualification-aiven-runtime')
ROOT.mkdir(exist_ok=True)


def emit(event, **data):
    print(json.dumps(dict(event=event, utc=time.time(), **data)), flush=True)


def disk():
    s = os.statvfs('.')
    return dict(filesystem_available_bytes=s.f_bavail*s.f_frsize, free_inodes=s.f_favail,
                runtime_bytes=sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file()))


def run(args, env=None):
    p = subprocess.run(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    if p.returncode:
        emit('runtime_step_failed', executable=Path(args[0]).name, returncode=p.returncode,
             disk_full=b'No space left on device' in p.stderr or b'ENOSPC' in p.stderr)
        raise RuntimeError('Runtime step failed')
    return p.stdout


try:
    emit('runtime_before', python=sys.version.split()[0], disk=disk())
    manifest = urllib.request.urlopen('https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt', timeout=30).read().decode()
    checksum, filename = next(line.split() for line in manifest.splitlines() if line.endswith('-linux-x64.tar.xz'))
    archive = ROOT / filename
    node_dir = ROOT / filename.removesuffix('.tar.xz')
    ready = ROOT / 'node_install_complete'
    if not ready.exists():
        urllib.request.urlretrieve('https://nodejs.org/dist/latest-v22.x/' + filename, archive)
        with archive.open('rb') as stream:
            assert hashlib.file_digest(stream, 'sha256').hexdigest() == checksum
        emit('node_archive_verified', archive_bytes=archive.stat().st_size, disk=disk())
        with tarfile.open(archive) as source:
            source.extractall(ROOT, filter='data')
        archive.unlink()
        run([str((node_dir/'bin/node').resolve()), '--version'])
        ready.write_text(filename)
    else:
        node_dir = ROOT / ready.read_text().strip().removesuffix('.tar.xz')
    env = {k:v for k,v in os.environ.items() if not any(t in k.upper() for t in ('SECRET','PASSWORD','TOKEN','DATABASE_URL','API_KEY','PG_'))}
    env['PATH'] = str((node_dir/'bin').resolve()) + os.pathsep + env.get('PATH','')
    env['npm_config_cache'] = str((ROOT/'npm-cache').resolve())
    env['PIP_NO_INPUT'] = '1'
    env['PIP_INDEX_URL'] = 'https://pypi.org/simple'
    gmgn_root = ROOT/'gmgn'
    run([str((node_dir/'bin/npm').resolve()), 'install', '--prefix', str(gmgn_root),
         '--no-audit', '--no-fund', 'gmgn-cli@1.6.4'], env)
    run([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', 'telethon', 'ijson>=3.3,<4'], env)
    node = run([str((node_dir/'bin/node').resolve()), '--version'], env).decode().strip()
    gmgn = run([str((gmgn_root/'node_modules/.bin/gmgn-cli').resolve()), '--version'], env).decode().strip()
    run([sys.executable, '-c', 'import telethon,dotenv,psycopg,ijson'], env)
    emit('runtime_ready', node=node, gmgn=gmgn, python_imports_ok=True,
         official_node_sha256_verified=True, disk=disk())
except Exception as exc:
    emit('runtime_failed', error_type=type(exc).__name__, disk=disk())
    sys.exit(1)
