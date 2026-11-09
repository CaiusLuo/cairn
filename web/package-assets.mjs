import { cpSync, mkdirSync, rmSync } from "node:fs";
import { fileURLToPath } from "node:url";

const source = fileURLToPath(new URL("./dist/", import.meta.url));
const target = fileURLToPath(new URL("../src/cairn/interfaces/web/static/", import.meta.url));
rmSync(target, { recursive: true, force: true });
mkdirSync(target, { recursive: true });
cpSync(source, target, { recursive: true });
