// 配信用のビルド(dist/)に、E2E 専用の偽物のサインインが入っていないことを確かめる(本体仕様書 §15.1 の M7)。
//   npm run build && node scripts/check-dist.mjs

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const MARKER = "E2E-ONLY-FAKE-AUTH";
const found = [];

function walk(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path);
    else if (readFileSync(path, "utf8").includes(MARKER)) found.push(path);
  }
}

walk("dist");
if (found.length > 0) {
  console.error(`配信用のビルドに、E2E 専用の偽物のサインインが入っています: ${found.join(", ")}`);
  process.exit(1);
}
console.log("配信用のビルドに、E2E 専用の偽物のサインインは入っていません");
