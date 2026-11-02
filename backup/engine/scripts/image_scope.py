"""Shared image exclusions: keep GPU runtimes and exclude exact Ollama aliases."""
import re

REFERENCE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_./:@-]{0,4095}\Z')
SHA256 = re.compile(r'sha256:[a-f0-9]{64}\Z')


def ollama_repository(reference):
    name = reference.split('@', 1)[0]
    parts = name.split('/')
    if len(parts) > 1 and ('.' in parts[0] or ':' in parts[0] or parts[0] == 'localhost'):
        parts = parts[1:]
    parts[-1] = parts[-1].split(':', 1)[0]
    return '/'.join(parts) == 'ollama/ollama'


def image_targets(references, table):
    expected = set(references)
    result = {}
    for line in table.splitlines():
        fields = line.split()
        if fields and fields[0] in expected:
            if len(fields) < 3 or not SHA256.fullmatch(fields[2]) or fields[0] in result:
                raise ValueError('Invalid or duplicate immutable image metadata')
            result[fields[0]] = fields[2]
    if set(result) != expected:
        raise ValueError('Complete image metadata required for scope exclusions')
    return result


def scope_images(references, targets=None):
    if (not isinstance(references, list) or not 0 < len(references) <= 10000 or
            any(not isinstance(r, str) or not REFERENCE.fullmatch(r) for r in references)):
        raise ValueError('Bounded valid image reference inventory required')
    excluded = {r for r in references if ollama_repository(r)}
    if excluded:
        if (not isinstance(targets, dict) or set(targets) != set(references) or
                any(not isinstance(v, str) or not SHA256.fullmatch(v) for v in targets.values())):
            raise ValueError('Complete image metadata required for scope exclusions')
        roots = {targets[r] for r in excluded}
        excluded.update(r for r in references if targets[r] in roots)
    selected = sorted(set(references) - excluded)
    if not selected:
        raise ValueError('No in-scope images remain')
    proof = {'policy': 'ollama-runtime-excluded.v1', 'excluded_references': sorted(excluded)}
    if targets is not None:
        if set(targets) != set(references) or any(not SHA256.fullmatch(value) for value in targets.values()):
            raise ValueError('Complete image metadata required')
        proof['targets'] = {ref: targets[ref] for ref in selected}
    return selected, proof
