// Local compatibility adapter; see provenance.json for the upstream source pin.
// Python/PyYAML are existing repository prerequisites, never installed here.
const PARSE = `import json, resource, sys
try:
    resource.setrlimit(resource.RLIMIT_AS, (128 * 1024**2, 128 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    import yaml
    raw = sys.stdin.buffer.read(1024**2 + 1)
    if len(raw) > 1024**2:
        raise ValueError()
    value = yaml.safe_load(raw)
    if not isinstance(value, dict):
        raise ValueError()
    encoded = json.dumps(value, allow_nan=False)
    if len(encoded.encode('utf-8')) > 2 * 1024**2:
        raise ValueError()
    sys.stdout.write(encoded)
except BaseException:
    sys.exit(2)
`;

export function parseConfigYAML(value: string): Record<string, unknown> {
  if (typeof value !== "string" || Buffer.byteLength(value, "utf8") > 1024 ** 2)
    throw new Error("native_yaml_input_invalid");
  try {
    const result = Bun.spawnSync(["python3", "-I", "-B", "-c", PARSE], {
      stdin: Buffer.from(value, "utf8"), stdout: "pipe", stderr: "pipe",
      env: { PATH: process.env.PATH || "/usr/bin:/bin" }, timeout: 5000,
    });
    if (result.exitCode !== 0 || result.stdout.length > 2 * 1024 ** 2)
      throw new Error("parse_failed");
    const parsed = JSON.parse(result.stdout.toString());
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
      throw new Error("mapping_required");
    return parsed;
  } catch {
    // Never echo YAML, credentials, parser exceptions or subprocess stderr.
    throw new Error("native_yaml_parser_unavailable_or_invalid");
  }
}
