#!/usr/bin/env python3
"""Repeatable post-restore safety checks inside the dedicated VM only."""

from runtime_config import parse_config, require

import json
import socket
import subprocess


def main():
    """Check readiness, suspended automation, private routing and internal DNS."""
    args, cfg = parse_config()
    cfg.require_guest()
    k = [*cfg.guest_kube, '--request-timeout=15s']

    def read(kind):
        return json.loads(subprocess.check_output(k + ['get', kind, '-A', '-o', 'json']))['items']

    workloads = read('deployment,statefulset')
    require(not any((x['metadata']['name'] == 'ollama' for x in workloads)), "Safety assertion failed")
    for name in ['openserverless-operator', 'kubegres-controller-manager']:
        require(next((x for x in workloads if x['metadata']['name'] == name))['spec']['replicas'] == 0, "Safety assertion failed")
    require(all((c['spec'].get('suspend') for c in read('cronjobs'))), "Safety assertion failed")
    pods = read('pods')
    running = [p for p in pods if p.get('status', {}).get('phase') != 'Succeeded']
    require(all((p.get('status', {}).get('phase') == 'Running' and all((c.get('ready') for c in p.get('status', {}).get('containerStatuses', []))) for p in running)), "Safety assertion failed")
    routes = json.loads(subprocess.check_output(['ip', '-j', 'route']))
    require(not any((x.get('dst') == 'default' for x in routes)), "Safety assertion failed")
    for target, port in [(cfg.guest['gateway'], 22), (cfg.source['node_ip'], 6443), ('1.1.1.1', 443)]:
        with socket.socket() as connection:
            connection.settimeout(2)
            try:
                connection.connect((target, port))
            except OSError:
                continue
            raise RuntimeError('Unexpected guest egress connection')
    dns = subprocess.check_output(k + ['exec', '-n', cfg.namespace, 'couchdb-0', '--',
                                     'getent', 'hosts', f'seaweedfs.{cfg.namespace}.svc.cluster.local'], text=True)
    service = json.loads(subprocess.check_output(k + ['get','service','seaweedfs','-n',cfg.namespace,'-o','json']))
    require(service['spec']['clusterIP'] in dns.split(), 'Recovered service DNS mismatch')
    result = {'allRunningPodsReady': True, 'runningPodCount': len(running), 'allCronJobsSuspended': True,
              'operatorReplicas': 0, 'kubegresOperatorReplicas': 0, 'ollamaDeploymentAbsent': True,
              'externalConnectionsBlocked': True, 'noDefaultRoute': True, 'internalServiceDnsWorks': True,
              'externalSsoTested': False, 'mailboxTested': False, 'xmlDeliveryTested': False}
    cfg.workspace_file('final-safety.json').write_text(json.dumps(result, indent=2) + '\n')
    print('Post-restore readiness and isolation checks passed:', len(running), 'Ready pods.')


if __name__ == '__main__':
    main()
