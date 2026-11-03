"""SeaweedFS 3.97 metadata framing, complete-load barrier and stable tree reads."""
from collections import deque
import json
from pathlib import Path
import os
import shutil
import socket
import struct
import time
import urllib.parse
import urllib.request

from runtime_config import require


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Unexpected metadata redirect')


def request_json(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(urllib.request.Request(url, headers={'Accept': 'application/json'}), timeout=30) as response:
        return json.load(response)


def tree(filer, get=request_json):
    pending, visited, entries = deque(['/']), set(), {}
    while pending:
        directory = pending.popleft()
        require(directory not in visited, 'Repeated filer directory')
        visited.add(directory); last = ''
        while True:
            data = get('http://' + filer + urllib.parse.quote(directory, safe='/') + '?'
                       + urllib.parse.urlencode({'limit': 1000, 'lastFileName': last}))
            batch = data.get('Entries') or []
            require(isinstance(batch, list) and len(batch) <= 1000, 'Invalid filer page')
            for entry in batch:
                path = entry['FullPath']
                require(path.startswith(directory) and path != directory and '/' not in path[len(directory):]
                        and not any(p in {'.', '..'} for p in path.split('/')), 'Invalid filer entry path')
                if path == '/topics/.system/log' or path.startswith('/topics/.system/log/'):
                    continue
                require(path not in entries, 'Duplicate filer entry')
                entries[path] = {k: entry.get(k) for k in ('Mode', 'FileSize', 'Mtime', 'Crtime', 'Uid', 'Gid',
                    'Mime', 'Replication', 'Collection', 'TtlSec', 'UserName', 'GroupNames', 'SymlinkTarget',
                    'Md5', 'Chunks', 'Content', 'Extended')}
                entries[path]['Mode'] = entry.get('Mode', 0)
                if entries[path]['Mode'] & (1 << 31):
                    pending.append(path + '/')
                require(len(entries) <= 1000000, 'Filer inventory limit exceeded')
            if not data.get('ShouldDisplayLoadMore'):
                break
            next_name = data.get('LastFileName') or (batch[-1]['FullPath'].rsplit('/', 1)[-1] if batch else '')
            require(next_name and next_name > last, 'Filer pagination stalled')
            last = next_name
    return entries


def fields(data):
    position, result = 0, {}
    def integer():
        nonlocal position
        value = 0
        for index in range(10):
            require(position < len(data), 'Truncated metadata varint')
            byte = data[position]; position += 1
            require(index != 9 or byte <= 1, 'Invalid metadata varint')
            value |= (byte & 127) << (index * 7)
            if not byte & 128:
                return value
        raise ValueError('Invalid metadata varint')
    while position < len(data):
        tag = integer(); number, wire = tag >> 3, tag & 7
        require(0 < number <= 0x1fffffff, 'Invalid metadata field')
        if wire == 0:
            value = integer()
        else:
            require(wire in {1, 2, 5}, 'Unsupported metadata wire type')
            size = integer() if wire == 2 else 8 if wire == 1 else 4
            require(position + size <= len(data), 'Truncated metadata field')
            value = data[position:position + size]; position += size
        result.setdefault(number, []).append((wire, value))
    return result


def one(rows, number, wire, default=None):
    values = rows.get(number, [])
    require(len(values) <= 1 and all(w == wire for w, unused in values), 'Ambiguous metadata identity')
    return values[0][1] if values else default


def metadata_records(path):
    records, seen = [], set()
    with Path(path).open('rb') as source:
        while True:
            offset = source.tell(); header = source.read(4)
            if not header:
                break
            require(len(header) == 4, 'Truncated metadata frame')
            size = struct.unpack('>I', header)[0]
            require(0 < size <= 4 * 1024 * 1024, 'Invalid metadata frame size')
            data = source.read(size)
            require(len(data) == size, 'Truncated metadata frame')
            full = fields(data)
            entry_bytes = one(full, 2, 2)
            require(entry_bytes is not None, 'Missing metadata entry')
            entry = fields(entry_bytes)
            parent = one(full, 1, 2, b'').decode('utf-8')
            name = one(entry, 1, 2, b'').decode('utf-8')
            directory = one(entry, 2, 0, 0)
            require(directory in {0, 1}, 'Invalid metadata entry type')
            require((not parent or parent.startswith('/')) and '//' not in parent and '/' not in name
                    and name not in {'.', '..'} and all(p not in {'.', '..'} for p in parent.split('/'))
                    and not any(ord(c) < 32 for c in parent + name) and (name or directory), 'Unsafe metadata path')
            name = (parent.rstrip('/') + '/' + name).rstrip('/') or '/'
            require(name not in seen, 'Duplicate metadata path')
            seen.add(name)
            records.append({'path': name, 'directory': bool(directory), 'offset': offset, 'size': size + 4})
            require(len(records) <= 1000001, 'Metadata record limit exceeded')
    require(bool(records), 'Empty metadata')
    return records


def prepare_complete_load(source, destination):
    """Preserve original frames, then wait for the last file RPC using a saved directory."""
    require(not Path(source).is_symlink(), 'Metadata symlink rejected')
    records = metadata_records(source)
    barrier = None
    if not records[-1]['directory']:
        parent = records[-1]['path'].rsplit('/', 1)[0] or '/'
        candidates = [r for r in records if r['directory'] and r['path'] != '/']
        require(bool(candidates), 'Saved non-root directory barrier unavailable')
        barrier = next((r for r in candidates if r['path'] == parent), candidates[0])
    # The exclusive destination is private; never edit the original archive.
    with Path(destination).open('xb') as output, Path(source).open('rb') as original:
        os.fchmod(output.fileno(), 0o600)
        shutil.copyfileobj(original, output, 1024 * 1024)
        if barrier:
            original.seek(barrier['offset']); output.write(original.read(barrier['size']))
    return {'files': sum(not r['directory'] for r in records), 'directoryBarrierAppended': barrier is not None}


def wait_endpoints(address, *, timeout=120, probe=None, clock=time.monotonic, pause=time.sleep):
    def connect(port):
        try:
            with socket.create_connection((address, port), timeout=1):
                return True
        except OSError:
            return False
    probe = probe or connect
    deadline = clock() + timeout
    while True:
        if all(probe(port) for port in (8080, 9090, 19090, 9333, 19333)):
            return
        require(clock() < deadline, 'Storage HTTP/RPC endpoints unavailable')
        pause(2)
