"""Public synthetic qualification only. No DMO source, state or credentials."""
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import resource
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

from dotenv import load_dotenv
import psycopg

VERSION = 'qualification-v2'
load_dotenv('.env')
STATE = Path('qualification_state')
STATE.mkdir(exist_ok=True)
MARKER = STATE / 'marker.json'
existing = MARKER.exists()
if existing:
    marker = json.loads(MARKER.read_text())
else:
    marker = {'uuid': str(uuid.uuid4())}
    with MARKER.open('w') as f:
        json.dump(marker, f)
        f.flush()
        os.fsync(f.fileno())
stop = threading.Event()
commands = queue.Queue()
conn = None

def emit(event, **data):
    print(json.dumps(dict(event=event, utc=time.time(), version=VERSION,
                         marker=marker['uuid'], **data), default=str), flush=True)

def metrics():
    result = {'rss_peak_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              'process_cpu_s': time.process_time()}
    for name in ('memory.current', 'memory.max', 'cpu.stat', 'cpu.max'):
        p = Path('/sys/fs/cgroup') / name
        if p.exists():
            result[name] = p.read_text().strip()
    result['files_bytes'] = sum(p.stat().st_size for p in STATE.rglob('*') if p.is_file())
    return result

def environments():
    # Log booleans only; neither dummy value nor credential is printed.
    result = {}
    for key in ('BOTKEEP_PROBE_SECRET', 'BOTKEEP_PROBE_ENV'):
        value = os.getenv(key, '')
        expected = os.getenv(key + '_SHA256', '')
        result[key + '_exists'] = bool(value)
        result[key + '_matches'] = bool(expected) and hashlib.sha256(value.encode()).hexdigest() == expected
    return result

def connect():
    global conn
    if conn is None or conn.closed:
        host = os.environ['PROBE_PG_HOST']
        port = int(os.environ['PROBE_PG_PORT'])
        addresses = sorted({x[4][0] for x in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        emit('dns_resolved', addresses=addresses, port=port, network_path='public_assigned_endpoint')
        with socket.create_connection((host, port), timeout=10):
            emit('tcp_connected')
        ca = Path('qualification-pg-ca.pem')
        ca.write_text(os.environ['PROBE_PG_CA_PEM'])
        conn = psycopg.connect(host=host, port=port, user=os.environ['PROBE_PG_USER'],
                              password=os.environ['PROBE_PG_PASSWORD'],
                              dbname=os.environ['PROBE_PG_DATABASE'], connect_timeout=10,
                              sslmode='verify-full', sslrootcert=str(ca),
                              options='-c statement_timeout=20000', autocommit=True)
        conn.execute('CREATE TABLE IF NOT EXISTS qualification_markers (id text PRIMARY KEY, version text, boots bigint NOT NULL)')
        with conn.transaction():
            conn.execute('INSERT INTO qualification_markers VALUES (%s,%s,1) ON CONFLICT(id) DO UPDATE SET version=excluded.version, boots=qualification_markers.boots+1', (marker['uuid'], VERSION))
        result = conn.execute('SELECT id,version,boots FROM qualification_markers WHERE id=%s', (marker['uuid'],)).fetchone()
        tls = conn.execute('SELECT ssl,version,cipher FROM pg_stat_ssl WHERE pid=pg_backend_pid()').fetchone()
        emit('db_committed_and_selected', same_uuid=result[0] == marker['uuid'], stored_version=result[1], boots=result[2], tls=tls)
    return conn

def db_metrics():
    c = connect()
    row = c.execute('SELECT pg_database_size(current_database()), pg_current_wal_lsn(), count(*) FROM qualification_markers').fetchone()
    return dict(database_bytes=row[0], wal_lsn=row[1], marker_rows=row[2])

def run_command(line):
    global conn
    words = line.strip().split()
    if not words:
        return
    if words[0] in ('verify', 'reconnect'):
        if conn is not None:
            conn.close()
        conn = None
        emit('verified', environment=environments(), database=db_metrics(), worker=metrics())
    elif words[0] == 'load':
        # Bounded synthetic memory/CPU/DB activity. Never reads the old payload table.
        seconds = min(60, max(1, int(words[1])))
        mb = min(512, max(1, int(words[2])))
        before = metrics()
        c = connect()
        c.execute('CREATE TABLE IF NOT EXISTS qualification_load (id int PRIMARY KEY, payload bytea)')
        buf = bytearray(mb * 1048576)
        for i in range(0, len(buf), 4096):
            buf[i] = 1
        start = time.monotonic()
        cpu_start = time.process_time()
        n = 0
        value = b'synthetic'
        while time.monotonic() - start < seconds:
            for _ in range(20000):
                value = hashlib.sha256(value).digest()
            with c.transaction():
                c.execute('INSERT INTO qualification_load VALUES (%s,%s) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (n % 128, os.urandom(65536)))
            n += 1
            if n % 25 == 0:
                emit('load_sample', worker=metrics(), database=db_metrics(), iterations=n)
        emit('load_complete', before=before, active=metrics(), database=db_metrics(), wall_s=time.monotonic()-start, cpu_s=time.process_time()-cpu_start, iterations=n)
        del buf
    elif words[0] == 'crash_once':
        flag = STATE / 'crash_once'
        if flag.exists():
            emit('crash_already_exercised')
            return
        with flag.open('w') as f:
            f.write(str(time.time()))
            f.flush()
            os.fsync(f.fileno())
        emit('intentional_crash', exit_code=17)
        os._exit(17)
    elif words[0] == 'metrics':
        emit('metrics', environment=environments(), worker=metrics(), database=db_metrics())
    else:
        emit('unknown_command')

def reader():
    for line in sys.stdin:
        commands.put(line)

signal.signal(signal.SIGTERM, lambda *_: stop.set())
threading.Thread(target=reader, daemon=True).start()
emit('boot', source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), python=platform.python_version(), marker_survived=existing,
     environment=environments(), worker=metrics(),
     node_available=shutil.which('node') is not None,
     gmgn_available=shutil.which('gmgn-cli') is not None)
last = 0
while not stop.is_set():
    try:
        run_command(commands.get(timeout=1))
    except queue.Empty:
        pass
    except Exception as exc:
        conn = None
        emit('command_error', error_type=type(exc).__name__, sqlstate=getattr(exc, 'sqlstate', None))
    if time.monotonic() - last >= 15:
        try:
            emit('heartbeat', environment=environments(), worker=metrics(), database=db_metrics())
        except Exception as exc:
            conn = None
            emit('db_unavailable', error_type=type(exc).__name__, sqlstate=getattr(exc, 'sqlstate', None))
        last = time.monotonic()
emit('shutdown')
