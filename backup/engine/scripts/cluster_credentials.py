"""Resolve existing service credentials in memory, without diagnostic payloads."""
import base64
import json
import subprocess

import yaml
from runtime_config import require


def environment(get, pod, name):
    spec = get('pod', pod)['spec']['containers'][0]
    entries = [e for e in spec.get('env', []) if e['name'] == name]
    require(len(entries) == 1, 'Ambiguous credential environment')
    entry = entries[0]
    if 'value' in entry:
        return entry['value']
    reference = entry['valueFrom']['secretKeyRef']
    return base64.b64decode(get('secret', reference['name'])['data'][reference['key']], validate=True).decode()


def milvus_password(command, namespace, *, read=None, resource_name='openserverless-milvus'):
    read = read or (lambda args: subprocess.check_output(args, timeout=25))
    # An authorization/transport error must not trigger a legacy fallback.
    raw = read([*command, '--request-timeout=20s', 'get', 'secret', resource_name,
                '-n', namespace, '--ignore-not-found', '-o', 'json'])
    if raw.strip():
        data = json.loads(raw)['data']['user.yaml']
        configuration = base64.b64decode(data, validate=True).decode()
    else:
        raw = read([*command, '--request-timeout=20s', 'get', 'configmap', resource_name,
                    '-n', namespace, '-o', 'json'])
        configuration = json.loads(raw)['data']['user.yaml']
    password = yaml.safe_load(configuration)['common']['security']['defaultRootPassword']
    require(isinstance(password, str) and password, 'Missing Milvus root credential')
    return password
