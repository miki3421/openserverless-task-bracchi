"""Online coverage derives from the existing k3s component/PVC contract."""
import json
import re

from . import check, docker, inventory as kind_inventory, fingerprint, KindError
from .profiles import profile, application_plan
from image_scope import ollama_repository, image_targets, scope_images as select_scoped_images

PODS = {'postgres': 'openserverless-postgres-1-0', 'couchdb': 'couchdb-0',
        'openwhisk-catalog': 'couchdb-0', 'redis': 'redis-0', 'redis-acl': 'redis-0',
        'application-etcd': 'openserverless-etcd-0', 'milvus': 'openserverless-milvus-0',
        'seaweedfs': 'seaweedfs-0'}
FILESYSTEM_PODS = {'registry-pvc': 'registry-0', 'openserverless-operator-deploy-pvc': 'openserverless-operator-0',
                   'openserverless-static-pvc': 'openserverless-static-0',
                   'openserverless-mongodb-pvc': 'openserverless-mongodb-0', 'tika-data': 'tika-0'}


def scope_images(cfg, io, references):
    # Bind every selected tag/alias to its immutable root, even without Ollama.
    text = io.execute(docker(cfg.kind, 'exec', cfg.source['container_id'], 'ctr', '--namespace', 'k8s.io',
                             'images', 'list')).decode()
    try:
        targets = image_targets(references, text)
    except ValueError as error:
        raise KindError(str(error)) from None
    try:
        return select_scoped_images(references, targets)
    except ValueError as error:
        raise KindError(str(error)) from None


def pod_pin(pod, node):
    check(pod['spec']['nodeName'] == node and not pod['metadata'].get('deletionTimestamp') and
          pod['status']['phase'] == 'Running', 'Expected running source pod required')
    states = pod['status'].get('containerStatuses', [])
    check(states and all(s['ready'] and 'running' in s['state'] and s.get('imageID') for s in states),
          'Ready immutable pod invocation required')
    return {'uid': pod['metadata']['uid'], 'spec_sha256': fingerprint(pod['spec']),
            'containers': [{'name': s['name'], 'image_id': s['imageID'], 'restarts': s['restartCount'],
                            'started_at': s['state']['running']['startedAt']} for s in states]}


def filesystem_mapping(pod, claim):
    volumes = [v['name'] for v in pod['spec']['volumes'] if v.get('persistentVolumeClaim', {}).get('claimName') == claim]
    mounts = [m for c in pod['spec']['containers'] for m in c.get('volumeMounts', []) if m['name'] in volumes]
    check(len(pod['spec']['containers']) == 1 and len(volumes) == 1 and len(mounts) == 1 and
          mounts[0]['mountPath'].startswith('/') and mounts[0]['mountPath'] != '/' and
          '..' not in mounts[0]['mountPath'].split('/') and not mounts[0].get('subPath') and
          not mounts[0].get('subPathExpr'), 'Ambiguous filesystem PVC mapping')
    return mounts[0]['mountPath']


def control_plane(cfg, io):
    name = 'etcd-' + cfg.source['node']
    pod = io.get('pod', name, 'kube-system')
    pin = pod_pin(pod, cfg.source['node'])
    containers = pod['spec']['containers']
    check(len(containers) == 1 and containers[0]['name'] == cfg.online['etcd_container'] and
          pod['spec'].get('hostNetwork') is True, 'Standard single control-plane etcd required')
    mounts = {m['name']: m['mountPath'] for m in containers[0]['volumeMounts']}
    hosts = {v['hostPath']['path']: mounts.get(v['name']) for v in pod['spec']['volumes'] if 'hostPath' in v}
    check(hosts.get('/var/lib/etcd') == '/var/lib/etcd' and
          hosts.get('/etc/kubernetes/pki/etcd') == '/etc/kubernetes/pki/etcd',
          'Standard pinned etcd data/certificate mounts required')
    command = containers[0].get('command', []) + containers[0].get('args', [])
    check('--data-dir=/var/lib/etcd' in command and
          '--trusted-ca-file=/etc/kubernetes/pki/etcd/ca.crt' in command,
          'Nonstandard control-plane etcd flags require qualification')
    return {'pod': name, 'pin': pin}


def control_plane_tools(cfg, io, pod):
    """Verify the explicitly configured snapshot reader without taking a snapshot."""
    prefix = io.kube('exec', '-n', 'kube-system', pod, '-c', cfg.online['etcd_container'], '--', timeout=30)
    io.execute([*prefix, 'etcdctl', 'version'])
    tool = cfg.online['etcd_status_tool']
    io.execute([*prefix, tool, 'version'])
    io.execute([*prefix, tool, 'snapshot', 'status', '--help'])


def observe(cfg, io):
    source = kind_inventory(cfg.kind, io)
    # Fail before collection on missing tools. No installation or helper pod.
    io.execute(docker(cfg.kind, 'exec', cfg.source['container_id'], 'sh', '-c',
                      'set -eu; command -v curl >/dev/null; command -v ctr >/dev/null; command -v tar >/dev/null; command -v df >/dev/null'))
    all_whisks = io.get(profile(cfg)['whisk_resource'])['items']
    selected = cfg.online['namespaces']
    check(sorted(w['metadata']['namespace'] for w in all_whisks) == sorted(selected),
          'Every standard OPS namespace must be explicitly selected')
    pvcs = io.get('pvc')['items']
    extra_settings = cfg.online.get('extra_pvcs', [])
    extra_keys = {(r['namespace'], r['name']) for r in extra_settings}
    check(all(r['namespace'] not in selected for r in extra_settings), 'Additional methods cannot bypass OPS backend coverage')
    exclusions = {(r['namespace'], r['name']) for r in cfg.online['excluded_pvcs']}
    actual_exclusions = {(p['metadata']['namespace'], p['metadata']['name']) for p in pvcs if p['metadata']['name'] == 'ollama-models'}
    check(exclusions == actual_exclusions, 'Model exclusions must match actual cluster inventory')
    check(not extra_keys & exclusions and extra_keys <= {(p['metadata']['namespace'], p['metadata']['name']) for p in pvcs},
          'Additional claim missing or excluded twice')
    check(all(p['metadata']['namespace'] in selected or
              (p['metadata']['namespace'], p['metadata']['name']) in exclusions | extra_keys for p in pvcs),
          'Unmapped external namespace PVC requires an additional collector')
    namespaces = {}
    for namespace in selected:
        obj = io.get('namespace', namespace)
        whisk = next(w for w in all_whisks if w['metadata']['namespace'] == namespace)
        claims = [p for p in pvcs if p['metadata']['namespace'] == namespace]
        plan = application_plan(cfg, whisk, claims, obj['metadata']['uid'])
        pods = io.get('pods', namespace=namespace)['items']
        owners, methods, fs = {}, {}, {}
        for claim, method in plan['pvcs'].items():
            candidates = [p for p in pods if any(v.get('persistentVolumeClaim', {}).get('claimName') == claim
                          for v in p['spec'].get('volumes', [])) and not p['metadata'].get('deletionTimestamp')]
            check(len(candidates) == 1, 'Exactly one source pod per data PVC required')
            name = candidates[0]['metadata']['name']; owners[name] = candidates[0]
            if method == 'other-pvcs':
                fs[claim] = {'pod': name}
            else:
                check(method not in methods or methods[method] == name, 'Ambiguous backend owner')
                methods[method] = name
        if 'redis-acl' in plan['requiredComponents']: methods['redis-acl'] = methods['redis']
        if 'openwhisk-catalog' in plan['requiredComponents']: methods['openwhisk-catalog'] = methods['couchdb']
        pins = {}
        for name, pod in sorted(owners.items()):
            pins[name] = pod_pin(pod, cfg.source['node'])
            check(len(pod['spec']['containers']) == 1, 'Multi-container collectors require explicit qualification')
            for claim, row in fs.items():
                if row['pod'] == name: row['mount'] = filesystem_mapping(pod, claim)
        if cfg.online.get('collector') == 'ops-native-v1':
            plan = {**plan, 'collector': 'ops-native-v1', 'nativePlan': plan,
                    'requiredComponents': ['ops-native-payload'],
                    'pvcs': {name: 'ops-native-payload' for name in plan['pvcs']}}
        namespaces[namespace] = {'plan': plan, 'pods': pins, 'method_pods': methods, 'filesystems': fs}
    images = io.execute(docker(cfg.kind, 'exec', cfg.source['container_id'], 'ctr', '--namespace', 'k8s.io',
                               'images', 'list', '-q')).decode().splitlines()
    images = sorted(set(images))
    check(images and all(i and not i.startswith('-') and not any(c.isspace() for c in i) for i in images),
          'Explicit nonempty image references required')
    images, image_scope = scope_images(cfg, io, images)
    extras = []
    if extra_settings:
        from .extra_pvcs import observe_extra
        io.execute(docker(cfg.kind, 'exec', cfg.source['container_id'], 'python3', '-c', 'import sqlite3, tarfile'))
        pvs = io.get('pv')['items']
        for row in sorted(extra_settings, key=lambda r: (r['namespace'], r['name'])):
            pvc = next(p for p in pvcs if p['metadata']['namespace'] == row['namespace'] and p['metadata']['name'] == row['name'])
            extras.append(observe_extra(cfg, io, row, pvc, pvs, io.get('pods', namespace=row['namespace'])['items']))
    return {'schema': 'ops-kind-online-inventory.v3', 'configuration_sha256': cfg.fingerprint,
            'kind': source, 'namespaces': namespaces, 'control_plane': control_plane(cfg, io),
            'excluded_pvcs': cfg.online['excluded_pvcs'], 'images': images, 'extra_pvcs': extras,
            'profile': cfg.online.get('profile', 'openserverless-v1'), 'image_scope': image_scope}


def make_plan(cfg, observed):
    check(isinstance(observed, dict) and set(observed) == {'schema', 'configuration_sha256', 'kind', 'namespaces',
          'control_plane', 'excluded_pvcs', 'images', 'extra_pvcs', 'profile', 'image_scope'} and observed['schema'] == 'ops-kind-online-inventory.v3' and
          observed['configuration_sha256'] == cfg.fingerprint and
          set(observed['namespaces']) == set(cfg.online['namespaces']) and
          observed['excluded_pvcs'] == cfg.online['excluded_pvcs'] and
          observed['profile'] == cfg.online.get('profile', 'openserverless-v1') and
          observed['image_scope'].get('policy') == 'ollama-runtime-excluded.v1' and
          not any(ollama_repository(r) for r in observed['images']) and
          len(observed['extra_pvcs']) == len(cfg.online.get('extra_pvcs', [])) and
          {(r['namespace'], r['claim'], r['method']) for r in observed['extra_pvcs']} ==
          {(r['namespace'], r['name'], r['method']) for r in cfg.online.get('extra_pvcs', [])}, 'Online inventory/configuration mismatch')
    value = {'schema': 'ops-kind-online-plan.v3', 'inventory': observed, 'outage_required': False,
             'contains_credentials': True, 'scope': 'kind-control-plane-listed-ops-namespaces-and-explicit-extra-pvcs',
             'external_services_included': False, 'application_restore_supported': False,
             'consistency': 'per-component native online backups; not a distributed atomic snapshot'}
    return {**value, 'digest': 'sha256:' + fingerprint(value)}


def validate_plan(cfg, plan, approval):
    check(isinstance(plan, dict) and 'inventory' in plan and plan == make_plan(cfg, plan['inventory']) and
          approval == plan['digest'], 'Exact online plan approval required')
