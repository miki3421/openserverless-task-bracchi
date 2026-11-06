"""Private, deadline/budget-bound online kind transport. No source lifecycle calls."""
import json
import copy
import math
import os
from pathlib import Path
import re
import resource
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

from . import KindConfig, check, docker, fingerprint, private_file, unique_object

LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')


class OnlineConfig:
    def __init__(self, data):
        check(isinstance(data, dict) and set(data) == {'version', 'kind', 'online'} and
              type(data['version']) is int and data['version'] == 1, 'Closed online kind configuration required')
        self.kind = KindConfig(data['kind'])
        self.source, self.root = self.kind.source, self.kind.root
        self.data = self.kind.data
        self.document = data
        self.online = data['online']
        required = {'namespaces', 'excluded_pvcs', 'etcd_container', 'etcd_status_tool'}
        check(isinstance(self.online, dict) and required <= set(self.online) <= required | {'profile', 'extra_pvcs', 'collector', 'image_archive', 'image_recovery_receipt', 'external_services'},
              'Closed online settings required')
        check(self.online.get('collector', 'classic-v1') in {'classic-v1', 'ops-native-v1'}, 'Unknown kind collector')
        if self.online.get('collector') == 'ops-native-v1':
            check(isinstance(self.online['namespaces'], list) and len(self.online['namespaces']) == 1,
                  'Native kind collection requires one explicit OPS installation')
        from .profiles import profile
        profile(self)
        namespaces = self.online['namespaces']
        check(isinstance(namespaces, list) and namespaces and len(namespaces) == len(set(namespaces)) and
              all(isinstance(n, str) and LABEL.fullmatch(n) and not n.startswith('set-') for n in namespaces),
              'Explicit OPS namespaces required')
        check(self.online['etcd_container'] == 'etcd' and self.online['etcd_status_tool'] in {'etcdctl', 'etcdutl'},
              'Explicit supported control-plane tools required')
        excluded = self.online['excluded_pvcs']
        check(isinstance(excluded, list), 'Explicit exclusions required')
        keys = set()
        for row in excluded:
            check(isinstance(row, dict) and set(row) == {'namespace', 'name', 'reason'} and
                  all(isinstance(row[k], str) and LABEL.fullmatch(row[k]) for k in ('namespace', 'name')) and
                  row['name'] == 'ollama-models' and isinstance(row['reason'], str) and
                  1 <= len(row['reason']) <= 500, 'Only explicit rebuildable model PVC exclusion supported')
            key = (row['namespace'], row['name'])
            check(key not in keys, 'Duplicate exclusion'); keys.add(key)
        if 'image_archive' in self.online:
            from image_archive import selection
            selection(self.online['image_archive'])
        if 'image_recovery_receipt' in self.online:
            from image_archive import selection
            check('image_archive' in self.online, 'Recovery receipt requires an explicit prepared image archive')
            selection(self.online['image_recovery_receipt'])
        if 'external_services' in self.online:
            from external_services import validate_settings
            validate_settings(self.online['external_services'])
        self.fingerprint = fingerprint(data)
        extras = self.online.get('extra_pvcs', [])
        check(isinstance(extras, list), 'Explicit additional claims required')
        keys = set()
        for row in extras:
            check(isinstance(row, dict) and set(row) == {'namespace', 'name', 'method'} and
                  all(isinstance(row[k], str) and LABEL.fullmatch(row[k]) for k in ('namespace', 'name')) and
                  row['method'] in {'sqlite-bundle', 'verified-empty', 'unbound-declaration'}, 'Closed additional PVC method required')
            key = (row['namespace'], row['name'])
            check(key not in keys, 'Duplicate additional PVC'); keys.add(key)

    @classmethod
    def load(cls, path):
        path = private_file(path)
        check(path.stat().st_size <= 65536, 'Configuration too large')
        return cls(json.loads(path.read_text(), object_pairs_hook=unique_object))

    def directory(self, *args, **kwargs):
        return self.kind.directory(*args, **kwargs)


class OnlineIO:
    def __init__(self, cfg, *, deadline=None, workspace=None, clock=time.monotonic):
        self.cfg, self.deadline, self.workspace, self.clock = cfg, deadline, workspace, clock

    def timeout(self, requested):
        if self.deadline is None:
            return requested
        remaining = self.deadline - self.clock()
        check(remaining > 0, 'Original online deadline reached')
        return min(requested, remaining)

    def execute(self, command, *, input_data=b'', output=None, timeout=30):
        # Even provider JSON is captured in a bounded private temporary file,
        # not an unbounded PIPE. stderr never enters model-visible diagnostics.
        timeout = self.timeout(timeout)
        limit = 8 * 1024**2
        if output is not None:
            check(self.workspace is not None and Path(output).is_relative_to(self.workspace), 'Output outside collection')
            used = sum(p.stat().st_size for p in self.workspace.rglob('*') if p.is_file())
            limit = self.cfg.data['storage']['max_capture_bytes'] - used
        check(limit > 0, 'Online artifact budget exhausted')
        def bound():
            resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
        try:
            stream = Path(output).open('xb') if output is not None else tempfile.TemporaryFile()
            with stream:
                os.fchmod(stream.fileno(), 0o600)
                subprocess.run(command, input=input_data, stdout=stream, stderr=subprocess.DEVNULL,
                               timeout=timeout, check=True, preexec_fn=bound)
                if output is None:
                    stream.seek(0); return stream.read(limit + 1)
                check(Path(output).stat().st_size > 0, 'Empty online export')
        except (subprocess.SubprocessError, OSError):
            raise ValueError('Online provider operation failed; inspect private workspace') from None

    def json(self, command, timeout=30):
        return json.loads(self.execute(command, timeout=timeout), object_pairs_hook=unique_object)

    def run(self, command, *, timeout, output=None, max_bytes=None):
        return self.execute(command, timeout=timeout, output=output)

    def kube(self, *arguments, timeout=20):
        seconds = max(1, math.ceil(self.timeout(timeout)))
        return ['kubectl', '--context', self.cfg.source['context'], '--request-timeout=' + str(seconds) + 's', *arguments]

    def get(self, resource, name=None, namespace=None):
        args = ['get', resource]
        if name is not None: args.append(name)
        args.extend(['-n', namespace] if namespace else ['--all-namespaces'] if resource not in {'nodes', 'namespace', 'pv'} else [])
        return self.json(self.kube(*args, '-o', 'json'))

    def pod(self, namespace, name, command, **kwargs):
        return self.execute(self.kube('exec', '-i', '-n', namespace, name, '--', *command,
                                     timeout=kwargs.get('timeout', 30)), **kwargs)

    def shell(self, namespace, pod, script, **kwargs):
        return self.pod(namespace, pod, ['sh', '-c', script], **kwargs)

    def cleanup(self, namespace, pod, directory, expected_uid):
        check(re.fullmatch(r'/tmp/ops-online-[a-f0-9]{32}', directory), 'Exact owned scratch path required')
        recovery = copy.copy(self)
        recovery.deadline = self.clock() + 30
        current = recovery.get('pod', pod, namespace)
        check(current['metadata']['uid'] == expected_uid, 'Do not clean a replacement pod')
        recovery.shell(namespace, pod, 'rm -rf -- ' + directory, timeout=30)

    def http(self, url, *, authorization=None, body=None):
        # Service/pod address is checked against current guarded inventory by
        # the caller. Curl runs inside the original node network, not the host.
        parsed = urlsplit(url)
        check(parsed.scheme == 'http' and parsed.hostname and not parsed.username and not parsed.password,
              'Explicit internal HTTP endpoint required')
        options = ['url = ' + json.dumps(url), 'header = "Accept: application/json"']
        if authorization:
            check(not any(c in authorization for c in ('\n', '\r', '\x00')), 'Invalid internal authorization')
            options.append('header = ' + json.dumps('Authorization: ' + authorization))
        if body is not None:
            options.extend(['header = "Content-Type: application/json"', 'data = ' + json.dumps(json.dumps(body))])
        command = docker(self.cfg.kind, 'exec', '-i', self.cfg.source['container_id'], 'curl',
                         '--disable', '--silent', '--show-error', '--fail', '--noproxy', '*',
                         '--proto', '=http', '--max-time', '25', '--config', '-')
        raw = self.execute(command, input_data=('\n'.join(options) + '\n').encode(), timeout=30)
        return json.loads(raw, object_pairs_hook=unique_object)
