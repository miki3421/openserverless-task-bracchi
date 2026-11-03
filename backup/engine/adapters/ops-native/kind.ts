// Same native collectors and payload contract, reached through a pinned kind network namespace.
import { preflight, collectAll, exportPayload } from "./cluster";
import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

export async function main() {
  const [operation, config, workspace, output] = process.argv.slice(2);
  if (!config || !workspace || !["preflight", "collect-all", "payload"].includes(operation) || (operation === "payload" && !output))
    throw new Error("invalid_kind_native_operation");
  return operation === "preflight" ? preflight(config, workspace, "kind") : operation === "collect-all" ?
    collectAll(config, workspace, "kind") : exportPayload(config, workspace, output);
}
if (import.meta.main) main().then(r => console.log(JSON.stringify(r))).catch(error => {
  // Provider details are retained only beside this private per-run request.
  const config = process.argv[3];
  if (config) try { writeFileSync(join(dirname(config), "native-error.private.json"),
    JSON.stringify({ code: error?.message || "kind_native_failed" }), { mode: 0o600 }); } catch {}
  console.error("Kind native phase failed; private evidence retained."); process.exitCode = 1;
});
