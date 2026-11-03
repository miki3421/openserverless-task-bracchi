"""Configured 1Password references; values stay in memory or private stdin."""
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess


def op_configured(refs, args, payload=None):
    path = Path(refs['token_file'])
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('private_1password_token_required')
    credential = path.read_text().strip()
    if credential.startswith('export '): credential = credential[7:].strip()
    if credential.startswith('OP_SERVICE_ACCOUNT_TOKEN='):
        credential = credential.split('=', 1)[1].strip().strip('\"\'')
    if not re.fullmatch(r'ops_[A-Za-z0-9_.-]+', credential):
        raise ValueError('invalid_configured_token')
    env = {k: v for k, v in os.environ.items() if not k.startswith('OP_')}
    env['OP_SERVICE_ACCOUNT_TOKEN'] = credential
    result = subprocess.run(['op', *args, '--format=json'], env=env,
                            input=None if payload is None else json.dumps(payload),
                            capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise ValueError('configured_1password_operation_failed')
    return json.loads(result.stdout)


def login_password(refs, expected_username):
    vault = op_configured(refs, ['vault', 'get', refs['vault']])['id']
    item = op_configured(refs, ['item', 'get', refs['sudo_item'], '--vault', vault, '--reveal'])
    if item.get('title') != refs['sudo_item'] or item.get('vault', {}).get('id') != vault:
        raise ValueError('sudo_item_identity_mismatch')
    users = [f.get('value') for f in item.get('fields', []) if f.get('id') == 'username']
    values = [f.get('value') for f in item.get('fields', []) if f.get('id') == 'password' and f.get('type') == 'CONCEALED']
    if users != [expected_username] or len(values) != 1 or not values[0] or any(c in values[0] for c in '\r\n\0'):
        raise ValueError('sudo_item_fields_invalid')
    return values[0]


def cluster_key(refs, *, create=False):
    vault = op_configured(refs, ['vault', 'get', refs['vault']])['id']
    matches = [i for i in op_configured(refs, ['item', 'list', '--vault', vault]) if i.get('title') == refs['key_item']]
    if len(matches) > 1: raise ValueError('duplicate_cluster_backup_key')
    expected = None
    if not matches:
        if not create: raise ValueError('cluster_backup_key_missing')
        expected = secrets.token_hex(32)
        item = op_configured(refs, ['item', 'create', '--vault', vault, '-'], {
            'title': refs['key_item'], 'category': 'SECURE_NOTE',
            'fields': [{'id': 'backup_key', 'label': 'backup_key', 'type': 'CONCEALED', 'value': expected}]})
        item_id = item['id']
    else: item_id = matches[0]['id']
    item = op_configured(refs, ['item', 'get', item_id, '--vault', vault, '--reveal'])
    if item.get('title') != refs['key_item'] or item.get('vault', {}).get('id') != vault:
        raise ValueError('cluster_key_identity_mismatch')
    fields = [f for f in item.get('fields', []) if f.get('value')]
    if (len(fields) != 1 or fields[0].get('label') != 'backup_key' or fields[0].get('type') != 'CONCEALED'
            or not re.fullmatch('[a-f0-9]{64}', fields[0].get('value', ''))):
        raise ValueError('cluster_key_invalid')
    value = fields[0]['value']
    if expected is not None and expected != value: raise ValueError('cluster_key_readback_mismatch')
    return value
