#!/usr/bin/env python3
"""Enable only lab application services; preserve suspended automation and no Ollama."""

from runtime_config import parse_config

import subprocess


def main():
    """Activate a fixed allowlist after native metadata/content verification."""
    args, cfg = parse_config()
    cfg.require_guest()
    import json
    report = json.loads(cfg.workspace_file('seaweed-full-verification.json').read_text())
    if report.get('result') != 'passed':
        raise SystemExit('SeaweedFS complete file verification required first')
    k = [*cfg.guest_kube]
    groups = [
        ('statefulset', cfg.namespace, ['openserverless-mongodb', 'controller', 'openserverless-static', 'openserverless-streamer-api']),
        ('deployment', cfg.namespace, ['openserverless-milvus-standalone', 'default-nginx']),
        ('deployment', 'kube-system', ['traefik']),
        ('deployment', 'cert-manager', ['cert-manager', 'cert-manager-cainjector', 'cert-manager-webhook']),
    ]
    for kind, namespace, names in groups:
        subprocess.run(k + ['scale', kind, *names, '-n', namespace, '--replicas=1'], check=True)
    # The system API is enabled by guest-offline-api-cache.py only after its
    # supplemental dependency cache is checksum-verified and mounted.
    print('Lab services enabled. Operator, Kubegres reconciler and cron remain disabled; no Ollama.')


if __name__ == '__main__':
    main()
