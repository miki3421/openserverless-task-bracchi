// Stream an already-opened, validated descriptor without reopening its pathname.
// Bun 1.2.5 stalls on the upstream fd-backed createReadStream in the reader tests.
import { closeSync, readSync } from "node:fs";

export async function* descriptorChunks(fd: number): AsyncGenerator<Uint8Array> {
  try {
    const block = Buffer.alloc(64 * 1024);
    for (;;) {
      const bytes = readSync(fd, block, 0, block.length, null);
      if (!bytes) return;
      // Yield independent bytes; consumers must not observe a reused buffer.
      yield Buffer.from(block.subarray(0, bytes));
    }
  } finally {
    closeSync(fd);
  }
}
