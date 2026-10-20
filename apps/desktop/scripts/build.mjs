#!/usr/bin/env node
/**
 * Build the desktop artifacts.
 *
 *   node scripts/build.mjs              main + preload + testable libraries + renderer
 *   node scripts/build.mjs --libs-only  only the main-process libraries the unit
 *                                       tests import (leaves the renderer alone)
 *
 * Electron's main and preload processes are CommonJS bundles, so every runtime
 * dependency (the generated @ravel/api-client included) is bundled and no
 * node_modules tree is required next to the packaged app. The renderer is a
 * normal Vite build with a relative base so it loads from file://.
 */

import { rm } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { build as esbuildBuild } from "esbuild";
import { build as viteBuild } from "vite";

const appDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const distDir = path.join(appDir, "dist");
const srcDir = path.join(appDir, "src");
const libsOnly = process.argv.includes("--libs-only");

const nodeBundle = {
  absWorkingDir: appDir,
  bundle: true,
  platform: "node",
  format: "cjs",
  target: "node22",
  external: ["electron"],
  logLevel: "info",
  // The renderer's own bundle decides its environment; the main process is
  // always a production build, including when a developer runs the unit tests.
  define: { "process.env.NODE_ENV": '"production"' },
};

const libraries = {
  "main/main": path.join(srcDir, "main", "main.ts"),
  "main/preload": path.join(srcDir, "preload", "preload.ts"),
  "lib/contract": path.join(srcDir, "shared", "contract.ts"),
  "lib/config": path.join(srcDir, "main", "config.ts"),
  "lib/security": path.join(srcDir, "main", "security.ts"),
  "lib/session-store": path.join(srcDir, "main", "session-store.ts"),
  "lib/gateway-service": path.join(srcDir, "main", "gateway-service.ts"),
};

async function buildMainProcess() {
  await esbuildBuild({
    ...nodeBundle,
    entryPoints: libraries,
    outdir: distDir,
    outExtension: { ".js": ".cjs" },
  });
}

async function buildRenderer() {
  await viteBuild({
    configFile: false,
    root: path.join(srcDir, "renderer"),
    base: "./",
    plugins: [react()],
    build: {
      outDir: path.join(distDir, "renderer"),
      emptyOutDir: true,
      target: "chrome130",
      sourcemap: false,
    },
    logLevel: "info",
  });
}

async function main() {
  if (!libsOnly) await rm(distDir, { recursive: true, force: true });
  await buildMainProcess();
  if (!libsOnly) await buildRenderer();
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
});
