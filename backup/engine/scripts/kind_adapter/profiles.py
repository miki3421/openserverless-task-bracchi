"""Explicit OPS layout translation; native completeness checks stay shared."""
import copy

from backup_manifest import collection_plan
from . import check

PROFILES = {'openserverless-v1': {'whisk_resource': 'whisks.openserverless.org', 'milvus_resource': 'openserverless-milvus'},
            'nuvolaris-legacy-v1': {'whisk_resource': 'whisks.nuvolaris.org', 'milvus_resource': 'nuvolaris-milvus'}}
LEGACY_CLAIMS = {
    'postgres-db-nuvolaris-postgres-1-0': 'postgres-db-openserverless-postgres-1-0',
    'nuvolaris-etc-data-nuvolaris-etcd-0': 'openserverless-etc-data-openserverless-etcd-0',
    'nuvolaris-milvus': 'openserverless-milvus',
    'nuvolaris-operator-deploy-pvc': 'openserverless-operator-deploy-pvc',
    'nuvolaris-static-pvc-nuvolaris-static-0': 'openserverless-static-pvc-nuvolaris-static-0',
    'nuvolaris-mongodb-pvc-nuvolaris-mongodb-0': 'openserverless-mongodb-pvc-nuvolaris-mongodb-0',
}


def profile(cfg):
    name = cfg.online.get('profile', 'openserverless-v1')
    check(name in PROFILES, 'Unknown OPS layout profile')
    return PROFILES[name]


def application_plan(cfg, whisk, pvcs, namespace_uid):
    name = cfg.online.get('profile', 'openserverless-v1')
    if name == 'openserverless-v1':
        return collection_plan(whisk, pvcs, namespace_uid, cfg.source['cluster_uid'], cfg.fingerprint)
    check(name == 'nuvolaris-legacy-v1', 'Explicit legacy profile required')
    translated, reverse = [], {}
    for pvc in pvcs:
        item = copy.deepcopy(pvc); actual = item['metadata']['name']
        canonical = LEGACY_CLAIMS.get(actual, actual)
        check(canonical not in reverse, 'Ambiguous legacy claim mapping')
        reverse[canonical] = actual; item['metadata']['name'] = canonical; translated.append(item)
    result = collection_plan(whisk, translated, namespace_uid, cfg.source['cluster_uid'], cfg.fingerprint)
    for key in ('pvcs', 'pvcUids'):
        result[key] = {reverse[k]: v for k, v in result[key].items()}
    result['layoutProfile'] = name
    return result
