// Licensed to the Apache Software Foundation (ASF) under one
// or more contributor license agreements. See the NOTICE file
// distributed with this work for additional information
// regarding copyright ownership. The ASF licenses this file
// to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance
// with the License. You may obtain a copy of the License at
// http://www.apache.org/licenses/LICENSE-2.0

import { Database } from "bun:sqlite";
import { closeSync, constants, fsyncSync, lstatSync, openSync, statfsSync, writeSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { createHash } from "node:crypto";
import { canonical } from "./releases";

export const INVENTORY_BYTES = 2 * 1024 ** 3;
const RESERVE_BYTES = 128 * 1024 ** 2;

function privateParent(path: string) {
  const parent = dirname(resolve(path)), info = lstatSync(parent);
  if (!info.isDirectory() || info.uid !== process.getuid?.() || (info.mode & 0o077))
    throw new Error("inventory_directory_not_private");
  for (let part = parent;; part = dirname(part)) {
    if (lstatSync(part).isSymbolicLink()) throw new Error("inventory_symlink_rejected");
    if (dirname(part) === part) break;
  }
}

/** Private bounded index. Neither directory breadth nor entry count lives in a JS map. */
export class DiskInventory {
  private db: Database;
  private cursor = 0;
  private logicalBytes = 0;
  private entries = 0;
  readonly maximumBytes: number;

  constructor(readonly path: string, maximumBytes = INVENTORY_BYTES) {
    if (!Number.isSafeInteger(maximumBytes) || maximumBytes < 1024 ** 2 || maximumBytes > INVENTORY_BYTES)
      throw new Error("invalid_inventory_budget");
    privateParent(path);
    const disk = statfsSync(dirname(path));
    if (disk.bavail * disk.bsize < maximumBytes + RESERVE_BYTES) throw new Error("insufficient_inventory_storage");
    const fd = openSync(path, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600);
    closeSync(fd);
    this.maximumBytes = maximumBytes;
    this.db = new Database(path);
    this.db.exec("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL; PRAGMA temp_store=FILE; PRAGMA cache_size=-2048;");
    this.db.exec("PRAGMA max_page_count=" + Math.floor(maximumBytes / 4096));
    this.db.exec("CREATE TABLE entries(path TEXT PRIMARY KEY COLLATE BINARY, value TEXT NOT NULL) WITHOUT ROWID;" +
      "CREATE TABLE directories(id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL);");
  }

  get count() { return this.entries; }

  batch(operation: () => void) {
    const previous = { bytes: this.logicalBytes, count: this.entries };
    try { return this.db.transaction(operation)(); }
    catch (error) { this.logicalBytes = previous.bytes; this.entries = previous.count; throw error; }
  }

  enqueue(directory: string) {
    if (typeof directory !== "string" || !directory.startsWith("/") || !directory.endsWith("/") || directory.includes("\0"))
      throw new Error("invalid_inventory_directory");
    try { this.db.query("INSERT INTO directories(path) VALUES (?)").run(directory); }
    catch { throw new Error("repeated_filer_directory_or_inventory_budget"); }
  }

  nextDirectory(): string | null {
    const row: any = this.db.query("SELECT id,path FROM directories WHERE id>? ORDER BY id LIMIT 1").get(this.cursor);
    if (!row) return null;
    this.cursor = row.id;
    return row.path;
  }

  add(path: string, value: any) {
    const text = canonical(value), bytes = Buffer.byteLength(JSON.stringify(path)) + Buffer.byteLength(text) + 2;
    if (this.logicalBytes + bytes > this.maximumBytes) throw new Error("filer_inventory_byte_budget_exceeded");
    try { this.db.query("INSERT INTO entries(path,value) VALUES (?,?)").run(path, text); }
    catch { throw new Error("duplicate_filer_entry_or_inventory_budget"); }
    this.logicalBytes += bytes;
    this.entries++;
  }

  compare(other: DiskInventory) {
    const left = this.db.query("SELECT path,value FROM entries ORDER BY path").iterate();
    const right = other.db.query("SELECT path,value FROM entries ORDER BY path").iterate();
    let a: any = left.next(), b: any = right.next(), addedEntries = 0, removedEntries = 0, changedEntries = 0;
    const changedSamples: { pathSha256: string; fields: string[] }[] = [];
    try {
      while (!a.done || !b.done) {
        const order = a.done ? 1 : b.done ? -1 : Buffer.compare(Buffer.from(a.value.path), Buffer.from(b.value.path));
        if (order < 0) { removedEntries++; a = left.next(); }
        else if (order > 0) { addedEntries++; b = right.next(); }
        else {
          if (a.value.value !== b.value.value) {
            changedEntries++;
            if (changedSamples.length < 20) {
              const before = JSON.parse(a.value.value), after = JSON.parse(b.value.value);
              changedSamples.push({ pathSha256: createHash("sha256").update(a.value.path).digest("hex"),
                fields: [...new Set([...Object.keys(before), ...Object.keys(after)])]
                  .filter(key => canonical(before[key] ?? null) !== canonical(after[key] ?? null)).sort() });
            }
          }
          a = left.next(); b = right.next();
        }
      }
    } finally { left.return?.(); right.return?.(); }
    return { addedEntries, removedEntries, changedEntries, changedSamples };
  }

  /** Preserve the original JSON-object manifest format for existing restore readers. */
  writeJSON(path: string) {
    privateParent(path);
    const fd = openSync(path, constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600);
    let buffer = "{", first = true;
    const flush = () => { if (buffer) { writeSync(fd, buffer); buffer = ""; } };
    try {
      for (const row of this.db.query("SELECT path,value FROM entries ORDER BY path").iterate() as Iterable<any>) {
        buffer += (first ? "" : ",") + JSON.stringify(row.path) + ":" + row.value;
        first = false;
        if (buffer.length >= 65536) flush();
      }
      buffer += "}\n"; flush(); fsyncSync(fd);
    } finally { closeSync(fd); }
  }

  close() { this.db.close(); }
}
