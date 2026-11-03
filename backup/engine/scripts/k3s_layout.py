"""Read-only standard K3s layout guard, shared by preflight and collection."""
import os
from pathlib import Path
import re
import subprocess


def validate_layout(command, environment, executable, configurations):
    if any(x.startswith(("--data-dir", "--datastore-endpoint", "--config")) or x == "-d" for x in command):
        raise ValueError('Explicit datastore/config/data-dir flags need inspection before collection')
    if any(environment.get(k) for k in ('K3S_DATASTORE_ENDPOINT', 'K3S_CONFIG_FILE')):
        raise ValueError('Explicit k3s datastore/config environment needs inspection')
    assets = environment.get('K3S_DATA_DIR')
    if assets:
        # Older multicall launchers set this to the unpacked binary asset directory,
        # not the server datastore. Require the exact standard path AND running exe.
        # Upstream: k3s v1.27.7+k3s2 cmd/k3s/main.go stageAndRun/getAssetAndDir.
        if not re.fullmatch(r'/var/lib/rancher/k3s/data/[0-9a-f]{64}', assets) or executable != assets + '/bin/k3s':
            raise ValueError('Explicit k3s data directory environment needs inspection')
    for text in configurations:
        # Parse actual mappings so quoted keys, JSON and YAML merges cannot hide
        # a custom datastore. No configuration is executed or reinterpreted as shell.
        import yaml
        class UniqueLoader(yaml.SafeLoader):
            pass
        def mapping(loader, node, deep=False):
            loader.flatten_mapping(node)
            result = {}
            for key, value in node.value:
                name = loader.construct_object(key, deep=deep)
                if name in result: raise ValueError('Ambiguous k3s configuration keys')
                result[name] = loader.construct_object(value, deep=deep)
            return result
        UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
        value = yaml.load(text, Loader=UniqueLoader)
        if value is not None and (not isinstance(value, dict) or {'data-dir', 'datastore-endpoint'} & value.keys()):
            raise ValueError('Configured datastore/data-dir needs inspection')
    return {'dataDirectory': '/var/lib/rancher/k3s', 'legacyBinaryAssetsDetected': bool(assets)}


def inspect_layout():
    pid = subprocess.check_output(['systemctl', 'show', 'k3s', '--property=MainPID', '--value'],
                                  text=True, timeout=30).strip()
    if not pid.isdigit() or int(pid) <= 1: raise ValueError('Active K3s process required')
    proc = Path('/proc') / pid
    command = proc.joinpath('cmdline').read_bytes().decode().split('\0')
    environment = dict(entry.split('=', 1) for entry in proc.joinpath('environ').read_bytes().decode().split('\0') if '=' in entry)
    paths = [Path('/etc/rancher/k3s/config.yaml'), *sorted(Path('/etc/rancher/k3s/config.yaml.d').glob('*.yaml'))]
    texts = []
    for path in paths:
        if path.is_symlink(): raise ValueError('Linked k3s configuration needs inspection')
        if path.exists():
            if not path.is_file() or path.stat().st_size > 65536: raise ValueError('Invalid k3s configuration file')
            texts.append(path.read_text())
    return validate_layout(command, environment, os.readlink(proc / 'exe'), texts)
