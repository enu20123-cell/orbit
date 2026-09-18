import { mkdir, readFile, writeFile } from "node:fs/promises";

const [template, styles, app, workerSource, hosting] = await Promise.all([
  readFile(new URL("./src/index.html", import.meta.url), "utf8"),
  readFile(new URL("./src/styles.css", import.meta.url), "utf8"),
  readFile(new URL("./src/app.js", import.meta.url), "utf8"),
  readFile(new URL("./server/index.js", import.meta.url), "utf8"),
  readFile(new URL("./.openai/hosting.json", import.meta.url), "utf8"),
]);

const html = template
  .replace("  <!-- ORBIT_STYLES -->", `  <style>\n${styles}  </style>`)
  .replace("  <!-- ORBIT_APP -->", `  <script>\n${app}  </script>`);

if (html === template || !workerSource.includes("__ORBIT_PAGE__")) {
  throw new Error("Build placeholders are missing");
}

const worker = workerSource.replace("__ORBIT_PAGE__", JSON.stringify(html));

await mkdir(new URL("./dist/server/", import.meta.url), { recursive: true });
await mkdir(new URL("./dist/.openai/", import.meta.url), { recursive: true });
await Promise.all([
  writeFile(new URL("./dist/index.html", import.meta.url), html),
  writeFile(new URL("./dist/server/index.js", import.meta.url), worker),
  writeFile(new URL("./dist/.openai/hosting.json", import.meta.url), hosting),
]);

console.log("Built ORBIT frontend and protected /api/plan worker");
