import { createRequire } from "node:module";
import path from "node:path";

const [projectDir] = process.argv.slice(2);
const require = createRequire(path.join(projectDir, "package.json"));
const { ESLint } = require("eslint");

const { files, rules, probes = [] } = JSON.parse(await Bun.stdin.text());
const eslint = new ESLint({ cwd: projectDir });
const output = {};
for (const file of files) {
  const absolute = path.join(projectDir, file);
  if (await eslint.isPathIgnored(absolute)) {
    output[file] = { ignored: true, rules: {} };
    continue;
  }
  const config = await eslint.calculateConfigForFile(absolute);
  const found = {};
  for (const rule of rules) {
    found[rule] = config?.rules?.[rule] ?? null;
  }
  output[file] = { ignored: false, rules: found };
}
const probeResults = [];
for (const probe of probes) {
  const absolute = path.join(projectDir, probe.filePath);
  if (await eslint.isPathIgnored(absolute)) {
    probeResults.push({ filePath: probe.filePath, ignored: true, messages: [] });
    continue;
  }
  const [result] = await eslint.lintText(probe.text, { filePath: absolute });
  const messages = (result?.messages ?? []).map(({ ruleId, line }) => ({ ruleId, line }));
  probeResults.push({ filePath: probe.filePath, ignored: false, messages });
}
process.stdout.write(JSON.stringify({ files: output, probes: probeResults }));
