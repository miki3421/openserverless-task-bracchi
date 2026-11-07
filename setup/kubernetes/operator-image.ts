// Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
/** Preserve the complete selected reference in legacy image + ':' + tag consumers. */
export function operatorImageParts(reference: string) {
  if (!reference || /\s|["'`$\\]/.test(reference)) throw new Error("Invalid operator image reference");
  const colon = reference.lastIndexOf(":");
  if (colon <= reference.lastIndexOf("/") || colon === reference.length - 1)
    throw new Error("Operator image requires an explicit tag or digest");
  if (reference.includes("@") && !/@sha256:[0-9a-f]{64}$/.test(reference))
    throw new Error("Invalid operator image digest");
  return {image: reference.slice(0, colon), tag: reference.slice(colon + 1)};
}
if (import.meta.main) {
  try {
    const parts = operatorImageParts(process.argv[3] || "");
    if (process.argv[2] !== "image" && process.argv[2] !== "tag") throw new Error("Expected image or tag");
    console.log(parts[process.argv[2] as "image" | "tag"]);
  } catch (error) {console.error((error as Error).message);process.exitCode = 1;}
}
