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
import urllib.error

ROOT = Path('.qualification-aiven-runtime')
ROOT.mkdir(exist_ok=True)


def emit(event, **data):
    print(json.dumps(dict(event=event, utc=time.time(), **data)), flush=True)


def disk():
    s = os.statvfs('.')
    files = [p for p in Path('.').rglob('*') if p.is_file() and not p.is_symlink()]
    return dict(filesystem_available_bytes=s.f_bavail*s.f_frsize, free_inodes=s.f_favail,
                application_files_bytes=sum(p.stat().st_size for p in files),
                application_allocated_bytes=sum(p.stat().st_blocks*512 for p in files),
                runtime_bytes=sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file()))


def run(args, env=None):
    p = subprocess.run(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    if p.returncode:
        emit('runtime_step_failed', executable=Path(args[0]).name, returncode=p.returncode,
             disk_full=b'No space left on device' in p.stderr or b'ENOSPC' in p.stderr)
        raise RuntimeError('Runtime step failed')
    return p.stdout


if '--diagnostics' in sys.argv:
    try:
        node_dir = ROOT / (ROOT/'node_install_complete').read_text().strip().removesuffix('.tar.xz')
        env = {k:v for k,v in os.environ.items() if not any(t in k.upper() for t in ('SECRET','PASSWORD','TOKEN','DATABASE_URL','API_KEY','PG_'))}
        env['PATH'] = str((node_dir/'bin').resolve()) + os.pathsep + env.get('PATH','')
        env['GMGN_RATE_LIMIT_AUTO_RETRY_MAX_WAIT_MS'] = '0'
        emit('application_disk', disk=disk())
        for provider, url in [('telegram','https://api.telegram.org/'),('gmgn','https://gmgn.ai/'),('dexscreener','https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112')]:
            started=time.monotonic()
            try:
                with urllib.request.urlopen(url, timeout=20) as r:
                    data=r.read(2_000_000)
                    emit('public_provider_https',provider=provider,status=r.status,latency_ms=(time.monotonic()-started)*1000,response_bytes=len(data))
            except urllib.error.HTTPError as e:
                emit('public_provider_https',provider=provider,status=e.code,latency_ms=(time.monotonic()-started)*1000)
            except Exception as e:
                emit('public_provider_https_error',provider=provider,error_type=type(e).__name__)
        started=time.monotonic()
        p=subprocess.run([str((ROOT/'gmgn/node_modules/.bin/gmgn-cli').resolve()),'token','info','--chain','sol','--address','So11111111111111111111111111111111111111112','--raw'],env=env,input=b'',stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        combined=(p.stdout+p.stderr).lower()
        emit('gmgn_readonly_acquisition',returncode=p.returncode,latency_ms=(time.monotonic()-started)*1000,response_bytes=len(p.stdout),authentication_required=any(s in combined for s in (b'api key',b'api_key',b'private key',b'private_key',b'not configured',b'unauthorized',b'authentication')))
    except Exception as e:
        emit('diagnostics_error',error_type=type(e).__name__)
    sys.exit(0)


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
