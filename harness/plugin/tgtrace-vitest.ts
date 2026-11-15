/**
 * Vitest setup file: record every NAMED function that actually ran, per test.
 *
 * The Vitest counterpart of `tgtrace.py` (#122). It is loaded into the TARGET
 * repo's test workers through a generated config (see
 * `testgraph/vitest_adapter.py`), so it imports nothing from testgraph and only
 * `vitest` plus Node built-ins.
 *
 * How: V8 precise coverage through `node:inspector`. Each `takePreciseCoverage`
 * returns call counts since the previous take and resets them, so a take in
 * `beforeEach` clears the slate and a take in `afterEach` is exactly what the
 * test body (plus its hooks) ran. Function granularity (`detailed: false`) is
 * enough for attribution, which matches journey ENTRY symbols by name.
 *
 * Configured by environment, like tgtrace.py:
 *   TGTRACE_OUT_DIR  directory for one part file per test file (required, else inert)
 *   TGTRACE_ROOT     only record functions defined under this directory
 *   TGTRACE_SKIP     colon-separated path fragments to ignore
 *                    (default: /node_modules/ .test. .spec. /__tests__/)
 *
 * Part file: {"root", "backend", "file", "tests": {"<id>": [["<relpath>", "<name>"], ...]}}
 * where <id> is `<relfile>::<describe> > ... > <title>`, the same id the
 * adapter builds from Vitest's JSON reporter.
 *
 * Known limits: anonymous functions are dropped (nothing to resolve them by
 * without source maps); tests marked `.concurrent` in one file share one
 * coverage window, so their traces can bleed into each other.
 */
import { Session } from "node:inspector/promises";
import { closeSync, fsyncSync, mkdirSync, openSync, renameSync, rmSync, writeSync } from "node:fs";
import { createHash } from "node:crypto";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, afterEach, beforeEach } from "vitest";

const OUT_DIR = process.env.TGTRACE_OUT_DIR;
const ROOT = process.env.TGTRACE_ROOT ?? process.cwd();
const SKIP = (process.env.TGTRACE_SKIP ?? "/node_modules/:.test.:.spec.:/__tests__/")
  .split(":")
  .filter(Boolean);

type Hit = [string, string];
type Task = { name: string; type?: string; suite?: Task; file?: { filepath?: string; name: string } };

function testId(task: Task): string {
  const filepath = task.file?.filepath ?? task.file?.name ?? "";
  const names: string[] = [task.name];
  for (let s = task.suite; s && s.type === "suite" && s.name !== task.file?.name; s = s.suite) {
    if (s.name) names.unshift(s.name);
  }
  return `${relative(ROOT, filepath).split(sep).join("/")}::${names.join(" > ")}`;
}

function scriptPath(url: string): string | null {
  if (!url.startsWith("file://")) return null;
  try {
    return fileURLToPath(url.split("?")[0]);
  } catch {
    return null;
  }
}

if (OUT_DIR) {
  const session = new Session();
  session.connect();
  await session.post("Profiler.enable");
  await session.post("Profiler.startPreciseCoverage", { callCount: true, detailed: false });

  const tests: Record<string, Hit[]> = {};
  let file = "";

  beforeEach(async () => {
    await session.post("Profiler.takePreciseCoverage"); // reset: drop setup-time calls
  });

  afterEach(async (ctx) => {
    const { result } = await session.post("Profiler.takePreciseCoverage");
    const task = ctx.task as unknown as Task;
    file = task.file?.filepath ?? file;
    const hits = new Map<string, Hit>();
    for (const script of result) {
      const abs = scriptPath(script.url);
      if (!abs || !abs.startsWith(ROOT + sep)) continue;
      const rel = relative(ROOT, abs).split(sep).join("/");
      if (SKIP.some((frag) => ("/" + rel).includes(frag))) continue;
      for (const fn of script.functions) {
        const count = fn.ranges[0]?.count ?? 0;
        if (count > 0 && fn.functionName) hits.set(`${rel}\0${fn.functionName}`, [rel, fn.functionName]);
      }
    }
    tests[testId(task)] = [...hits.values()].sort();
  });

  afterAll(async () => {
    await session.post("Profiler.stopPreciseCoverage");
    session.disconnect();
    mkdirSync(OUT_DIR, { recursive: true });
    const name = createHash("sha1").update(file).digest("hex").slice(0, 16);
    const target = join(OUT_DIR, `${name}.json`);
    const tmp = `${target}.tmp.${process.pid}`;
    const payload = { root: ROOT, backend: "v8-precise-coverage", file: relative(ROOT, file), tests };
    // Whole or absent, as tgtrace.py writes: a worker killed mid-write must
    // not leave a truncated part for the adapter to merge.
    try {
      const fd = openSync(tmp, "w");
      try {
        writeSync(fd, JSON.stringify(payload, null, 1));
        fsyncSync(fd);
      } finally {
        closeSync(fd);
      }
      renameSync(tmp, target);
    } catch (err) {
      rmSync(tmp, { force: true });
      throw err;
    }
  });
}
