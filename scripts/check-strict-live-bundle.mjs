#!/usr/bin/env node
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const repoRoot = path.resolve(import.meta.dirname, "..");
const dist = path.join(repoRoot, "artifacts", "control-tower", "dist", "public");
const forbidden = [
  ["SimProvider", "simulation React provider"],
  ["/api/scenario", "scenario control API"],
  ["/api/events", "scenario event stream"],
  ["/api/actions", "scenario action queue"],
  ["SIMULATED JUDGE", "generated judge copy"],
  ["Simulation Demo", "simulation UI copy"],
  ["DEMO-FULL", "baked scenario identifier"],
  ["stub-portfolio", "generated portfolio fixture"],
  ["baked timeline", "generated action timeline copy"],
];

async function filesUnder(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const full = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...await filesUnder(full));
    else if (/\.(?:html|js|css|json|map)$/i.test(entry.name)) files.push(full);
  }
  return files;
}

let files;
try {
  files = await filesUnder(dist);
} catch (error) {
  console.error(`[strict-live] bundle not found at ${dist}; run the live build first`);
  throw error;
}

const violations = [];
for (const file of files) {
  const contents = await readFile(file, "utf8");
  for (const [needle, description] of forbidden) {
    if (contents.includes(needle)) violations.push(`${path.relative(repoRoot, file)}: ${description} (${needle})`);
  }
}

if (violations.length) {
  console.error("[strict-live] forbidden generated/demo artifacts were found in the production bundle:");
  for (const violation of violations) console.error(` - ${violation}`);
  process.exit(1);
}

console.log(`[strict-live] verified ${files.length} production bundle files; no generated/demo control plane found`);
