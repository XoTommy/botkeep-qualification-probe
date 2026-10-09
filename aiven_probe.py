"""Synthetic Botkeep/Aiven qualification. Never log connection fields or exceptions."""
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import resource
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

from dotenv import load_dotenv
import psycopg
from psycopg.conninfo import conninfo_to_dict

load_dotenv('.env')
STATE = Path('qualification_state')
STATE.mkdir(exist_ok=True)
MARKER = STATE / 'aiven_marker.json'
SURVIVED = MARKER.exists()
marker = json.loads(MARKER.read_text()) if SURVIVED else {'uuid': str(uuid.uuid4())}
if not SURVIVED:
    with MARKER.open('w') as f:
        json.dump(marker, f)
        f.flush()
        os.fsync(f.fileno())
stop = threading.Event()
commands = queue.Queue()


def emit(event, **data):
    print(json.dumps(dict(event=event, utc=time.time(), version='aiven-qualification-v1',
                         marker=marker['uuid'], **data)), flush=True)


def metrics():
    result = {'rss_peak_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              'process_cpu_s': time.process_time()}
    for name in ('memory.current', 'memory.max', 'memory.events', 'cpu.stat', 'cpu.max'):
        p = Path('/sys/fs/cgroup') / name
        if p.exists():
            result[name] = p.read_text().strip()
    disk = os.statvfs('.')
    result.update(filesystem_available_bytes=disk.f_bavail * disk.f_frsize,
                  filesystem_free_inodes=disk.f_favail,
                  qualification_files_bytes=sum(p.stat().st_size for p in STATE.rglob('*') if p.is_file()))
    return result


def connect():
    uri = os.environ['AIVEN_DATABASE_URL']
    params = conninfo_to_dict(uri)
    started = time.monotonic()
    addresses = socket.getaddrinfo(params['host'], int(params['port']), type=socket.SOCK_STREAM)
    emit('dns_pass', latency_ms=(time.monotonic()-started)*1000, address_count=len(addresses))
    started = time.monotonic()
    with socket.create_connection((params['host'], int(params['port'])), timeout=10):
        pass
    emit('tcp_pass', latency_ms=(time.monotonic()-started)*1000)
    ca = STATE / 'aiven_ca.pem'
    ca.write_text(os.environ['AIVEN_CA_PEM'].replace('\\n', '\n'))
    os.chmod(ca, 0o600)
    started = time.monotonic()
    c = psycopg.connect(uri, sslmode='verify-full', sslrootcert=str(ca), connect_timeout=10,
                       options='-c statement_timeout=20000', autocommit=True)
    emit('authenticated_tls_connection', latency_ms=(time.monotonic()-started)*1000)
    return c


def verify():
    with connect() as c:
        started = time.monotonic()
        with c.transaction():
            c.execute('CREATE TABLE IF NOT EXISTS botkeep_aiven_qualification (id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT clock_timestamp())')
            c.execute('INSERT INTO botkeep_aiven_qualification(id) VALUES (%s) ON CONFLICT(id) DO NOTHING', (marker['uuid'],))
        row = c.execute('SELECT id,created_at FROM botkeep_aiven_qualification WHERE id=%s', (marker['uuid'],)).fetchone()
        count = c.execute('SELECT count(*) FROM botkeep_aiven_qualification WHERE id=%s', (marker['uuid'],)).fetchone()[0]
        tls = c.execute('SELECT ssl,version FROM pg_stat_ssl WHERE pid=pg_backend_pid()').fetchone()
        emit('commit_read_pass', same_uuid=str(row[0]) == marker['uuid'], row_count=count,
             duplicate_free=count == 1, created_at=row[1].isoformat(), latency_ms=(time.monotonic()-started)*1000,
             tls_enabled=tls[0], tls_version=tls[1])
        settings = c.execute("SELECT name,setting,unit FROM pg_settings WHERE name IN ('server_version','max_connections','max_wal_size','min_wal_size','wal_segment_size','archive_timeout','checkpoint_timeout','default_transaction_read_only') ORDER BY name").fetchall()
        emit('db_configuration', settings=settings, database_bytes=c.execute('SELECT pg_database_size(current_database())').fetchone()[0])
    with connect() as c:
        started = time.monotonic()
        row = c.execute('SELECT id,created_at FROM botkeep_aiven_qualification WHERE id=%s', (marker['uuid'],)).fetchone()
        emit('reconnect_read_pass', same_uuid=str(row[0]) == marker['uuid'], latency_ms=(time.monotonic()-started)*1000,
             created_at=row[1].isoformat())


def load_worker(seconds=60, mib=512):
    seconds = min(60, max(1, seconds))
    mib = min(512, max(1, mib))
    before = metrics()
    buf = bytearray(mib * 1048576)
    for i in range(0, len(buf), 4096):
        buf[i] = 1
    started, cpu = time.monotonic(), time.process_time()
    value = b'synthetic'
    while time.monotonic()-started < seconds:
        tick = time.monotonic()
        busy = time.process_time()
        while time.process_time()-busy < 0.04:
            value = hashlib.sha256(value).digest()
        time.sleep(max(0, 0.1-(time.monotonic()-tick)))
    elapsed = time.monotonic()-started
    emit('worker_load_complete', before=before, active=metrics(), wall_s=elapsed,
         cpu_s=time.process_time()-cpu, target_cpu_vcpu=0.4, allocated_buffer_mib=mib)


def command(text):
    words = text.strip().split()
    if not words:
        return
    if words[0] in ('verify', 'reconnect'):
        verify()
    elif words[0] == 'cycles':
        for _ in range(3):
            verify()
            time.sleep(2)
    elif words[0] == 'metrics':
        emit('metrics', worker=metrics())
    elif words[0] == 'runtime':
        result = subprocess.run([sys.executable, '-u', 'aiven_runtime_probe.py'], timeout=300,
                                env={k:v for k,v in os.environ.items() if not any(t in k.upper() for t in ('SECRET','PASSWORD','TOKEN','DATABASE_URL','API_KEY','PG_'))})
        emit('runtime_command_finished', returncode=result.returncode)
    elif words[0] == 'load_worker':
        load_worker()
    elif words[0] == 'crash_once':
        flag = STATE / 'aiven_crash_once'
        if flag.exists():
            emit('crash_already_exercised')
        else:
            with flag.open('w') as f:
                f.write(str(time.time()))
                f.flush()
                os.fsync(f.fileno())
            emit('intentional_crash', exit_code=17)
            os._exit(17)
    else:
        emit('unknown_command')


def safe_call(fn):
    try:
        fn()
    except Exception as exc:
        # Never emit str(exc), traceback, connection info, subprocess output or environment.
        emit('qualification_error', error_type=type(exc).__name__, sqlstate=getattr(exc, 'sqlstate', None))


def reader():
    for line in sys.stdin:
        commands.put(line)


signal.signal(signal.SIGTERM, lambda *_: stop.set())
threading.Thread(target=reader, daemon=True).start()
emit('boot', python=platform.python_version(), marker_survived=SURVIVED,
     source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
     secret_present=bool(os.getenv('AIVEN_DATABASE_URL')), ca_present=bool(os.getenv('AIVEN_CA_PEM')), worker=metrics())
if os.getenv('AIVEN_DATABASE_URL') and os.getenv('AIVEN_CA_PEM'):
    safe_call(verify)
last_heartbeat, last_db = time.monotonic(), time.monotonic()
while not stop.is_set():
    try:
        line = commands.get(timeout=1)
        safe_call(lambda: command(line))
    except queue.Empty:
        pass
    now = time.monotonic()
    if now-last_heartbeat >= 60:
        emit('worker_heartbeat', worker=metrics())
        last_heartbeat = now
    # One representative health/write/read cycle per five minutes; no idle-bypass loop.
    if now-last_db >= 300 and os.getenv('AIVEN_DATABASE_URL') and os.getenv('AIVEN_CA_PEM'):
        safe_call(verify)
        last_db = now
emit('shutdown')
