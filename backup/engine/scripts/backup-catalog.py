#!/usr/bin/env python3
"""Offline source/capability catalogue, NOT a cluster inventory or backup runner.

No runtime configuration, credentials, environment, files or external commands
are read by the catalogue. Profiles describe inspected source implementations,
not detection of an installed product or permission to execute its commands.
"""
import argparse
from copy import deepcopy
import json


PROFILES = {
    'k3s-native': {
        'kind': 'cluster-building-block',
        'implementation': 'local',
        'source': 'https://github.com/miki3421/ops-k3s-backup',
        'revision': '4f9618e74d196b393a08776989ce2b2968fcb771',
        'scope': 'Standard single-node k3s control plane and selected OPS services.',
        'reuse': [
            'Native SQLite or embedded-etcd control-plane collection.',
            'PostgreSQL instance/WAL, CouchDB, Redis RDB/ACL, application etcd.',
            'SeaweedFS data and metadata, selected filesystem PVCs.',
            'Restic encryption, full read check and staging hash comparison.',
        ],
        'gaps': [
            'Unknown namespaces, PVCs and hostPaths are not fully covered.',
            'Ollama/model data and some cached images are excluded.',
            'Nonempty Milvus backup is not implemented here.',
            'Existing recovery is ARM64/SQLite-specific; other restores unqualified.',
        ],
    },
    'ops-native': {
        'kind': 'installation-building-block',
        'implementation': 'local-adapted-source',
        'source': 'https://github.com/miki3421/openserverless-task-custom',
        'revision': '1e681b28fd54e4132be1a233f8e246fa98c2c8a2',
        'source_path': 'update/',
        'local_path': 'adapters/ops-native/',
        'scope': 'All tenants/services inside the explicitly selected OPS installation.',
        'reuse': [
            'Inventory, actual owner/PVC identities, native component plans/receipts.',
            'Platform definitions, all OpenWhisk tenants and service data.',
            'Native SeaweedFS and Milvus collectors with qualified tools.',
            'Restic pack/unpack and offline payload validation.',
            'Recovery IAM and SeaweedFS metadata safety fixes.',
        ],
        'gaps': [
            'The selected OPS installation is not the entire Kubernetes cluster.',
            'Source is extracted locally; upstream wrappers have not adopted it yet.',
            'Operational wrappers and complete-cluster adapters are not qualified.',
            'The inspected candidate is not proof of installed CLI/task compatibility.',
            'Kind control-plane recovery and every architecture need qualification.',
        ],
    },
    'bestia-v2': {
        'kind': 'installation-building-block',
        'implementation': 'external-local-plugin',
        'source': 'Installed olaris-bestia backup plugin (no verified Git provenance).',
        'revision': None,
        'scope': 'Bestia system/application backup and restore task flows.',
        'reuse': [
            'Existing system-backup-v2 and system-restore-v2 task contracts.',
            'Existing list-backups and restore-preview-v2 task contracts.',
        ],
        'gaps': [
            'Source revision must be established before extraction or execution.',
            'Backup may install/configure Velero and an in-cluster storage service.',
            'Full restore can delete application namespaces and reinstall software.',
            'Native database consistency and whole-cluster coverage need verification.',
        ],
    },
    'workflow-logical': {
        'kind': 'application-export',
        'implementation': 'external-project-source',
        'source': 'https://github.com/gitveryta/veryta-mentor-workflow',
        'revision': '989b91d58f87c2d6c71d786c911c6888fa33640e',
        'source_path': 'scripts/backup_capture.py',
        'scope': 'One application database, S3 prefix and environment file.',
        'reuse': [
            'Existing GPG archive format/verification and 1Password key custody.',
            'Streaming custom database dump and bounded S3/file capture.',
            'Isolated synthetic restore procedures and explicit quiescent-window contract.',
        ],
        'gaps': [
            'Application export is not a complete cluster or OPS installation backup.',
            'Existing format must remain readable; do not silently convert old archives.',
            'Do not transplant application-specific credentials/config into the shared core.',
        ],
    },
}


def describe(profile):
    result = deepcopy(PROFILES[profile])
    result.update({
        'profile': profile,
        'schema_version': 1,
        'evidence': 'static-source-inspection',
        'target_inspected': False,
        'whole_cluster_backup_certified': False,
        'execution_supported_by_catalogue': False,
    })
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list', help='List inspected source profiles offline (JSON).')
    show = sub.add_parser('show', help='Show scope, provenance and gaps offline (JSON).')
    show.add_argument('profile', choices=sorted(PROFILES))
    args = parser.parse_args(argv)
    if args.command == 'list':
        result = {'schema_version': 1, 'profiles': [describe(p) for p in sorted(PROFILES)]}
    else:
        result = describe(args.profile)
    print(json.dumps(result, indent=2, ensure_ascii=True))


if __name__ == '__main__':
    main()
