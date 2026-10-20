"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");

const { appRoot, requireDist, readSource } = require("./support.cjs");

const contract = requireDist("contract");
const repoRoot = path.resolve(appRoot, "..", "..");
const mainSource = readSource("src", "main", "main.ts");
const securitySource = readSource("src", "main", "security.ts");
const configSource = readSource("src", "main", "config.ts");
const serviceSource = readSource("src", "main", "gateway-service.ts");
const sessionSource = readSource("src", "main", "session-store.ts");
const preloadSource = readSource("src", "preload", "preload.ts");
const appSource = readSource("src", "renderer", "src", "App.tsx");
const mainTsxSource = readSource("src", "renderer", "src", "main.tsx");
const indexHtml = readSource("src", "renderer", "index.html");
const manifest = JSON.parse(readSource("package.json"));

test("the named IPC allowlist is fixed and complete", () => {
  assert.deepEqual(contract.IPC_CHANNELS, {
    login: "ravel:auth:login",
    logout: "ravel:auth:logout",
    session: "ravel:session:read",
    listProjects: "ravel:projects:list",
    connection: "ravel:connection:read",
    reconnect: "ravel:connection:reconnect",
    connectionStatus: "ravel:connection:status",
  });
  assert.deepEqual([...contract.INVOKE_CHANNELS], [
    "ravel:auth:login",
    "ravel:auth:logout",
    "ravel:session:read",
    "ravel:projects:list",
    "ravel:connection:read",
    "ravel:connection:reconnect",
  ]);
  assert.deepEqual([...contract.EVENT_CHANNELS], ["ravel:connection:status"]);
  assert.equal(Object.isFrozen(contract.IPC_CHANNELS), true);
  assert.equal(Object.isFrozen(contract.INVOKE_CHANNELS), true);
  assert.equal(Object.isFrozen(contract.EVENT_CHANNELS), true);
  assert.equal(contract.isInvokeChannel("ravel:auth:login"), true);
  for (const name of ["", "ravel:shell", "shell.openExternal", "ravel:auth:register"]) {
    assert.equal(contract.isInvokeChannel(name), false, name);
  }
  assert.equal(contract.BRIDGE_VERSION, 1);
  assert.equal(contract.BRIDGE_GLOBAL, "ravelDesktop");
});

test("the main process registers exactly the six invoke channels and no generic IPC", () => {
  const registrations = [...mainSource.matchAll(/registerHandler\(\s*IPC_CHANNELS\.(\w+)/g)].map(
    (match) => match[1],
  );
  assert.deepEqual(registrations, [
    "login",
    "logout",
    "session",
    "listProjects",
    "connection",
    "reconnect",
  ]);
  assert.equal(/\bipcMain\.on\b/.test(mainSource), false);
  assert.equal(/\bipcMain\.handle\s*\(\s*(?!channel)/.test(mainSource), false);
  assert.equal(/ipcRenderer/.test(mainSource), false);
  assert.equal(/webContents\.send\s*\(\s*IPC_CHANNELS\.connectionStatus/.test(mainSource), true);
});

test("every handler refuses an untrusted sender before doing work", () => {
  assert.match(mainSource, /function registerHandler<[\s\S]*?assertTrustedSender\(event\)/);
  assert.match(mainSource, /sender:\s*event\.sender,\s*senderFrame:\s*event\.senderFrame/);
  assert.match(mainSource, /windowOpenDecision/);
  assert.match(mainSource, /permissionRequestAllowed/);
  assert.equal(/shell\.openExternal|openExternal\(/.test(mainSource), false);
  assert.equal(/\bshell\s*[,}]/.test(mainSource), false);
  assert.equal(/child_process|execFile|spawn\(/.test(mainSource), false);
  assert.equal(/nodeIntegration\s*:\s*true/.test(mainSource), false);
  assert.equal(/remote\s*:\s*true/.test(mainSource), false);
  assert.equal(/enableRemoteModule/.test(mainSource), false);
});

test("the window is created only through the hardened preferences", () => {
  assert.match(mainSource, /new BrowserWindow\(\{/);
  assert.match(mainSource, /webPreferences:\s*\{\s*\.\.\.secureWebPreferences\(/);
  for (const setting of [
    "contextIsolation: true",
    "nodeIntegration: false",
    "nodeIntegrationInWorker: false",
    "nodeIntegrationInSubFrames: false",
    "sandbox: true",
    "webSecurity: true",
    "allowRunningInsecureContent: false",
    "webviewTag: false",
    "navigateOnDragDrop: false",
  ]) {
    assert.equal(securitySource.includes(setting), true, setting);
  }
  assert.match(securitySource, /setWindowOpenHandler|windowOpenDecision/);
});

test("the preload exposes only the explicit bridge methods", () => {
  assert.match(preloadSource, /contextBridge\.exposeInMainWorld\(BRIDGE_GLOBAL, api\)/);
  for (const method of [
    "login",
    "logout",
    "session",
    "listProjects",
    "connection",
    "reconnect",
    "onConnectionStatus",
  ]) {
    assert.match(preloadSource, new RegExp(`${method}:`), method);
  }
  const invokes = preloadSource.match(/ipcRenderer\.invoke\(/g) ?? [];
  assert.equal(invokes.length, 6);
  assert.equal(
    (preloadSource.match(/ipcRenderer\.invoke\(\s*IPC_CHANNELS\.\w+/g) ?? []).length,
    6,
  );
  assert.equal(/ipcRenderer\.send\b/.test(preloadSource), false);
  assert.equal(/\brequire\s*\(/.test(preloadSource), false);
  assert.equal(/node:fs|node:child_process|node:process|process\.env|\bwebFrame\b/.test(preloadSource), false);
  assert.equal(/invoke\s*:\s*\(|send\s*:/.test(preloadSource), false);
});

test("the renderer has no credential store, no network escape and no node surface", () => {
  for (const forbidden of [
    "localStorage",
    "sessionStorage",
    "indexedDB",
    "document.cookie",
    "dangerouslySetInnerHTML",
    "eval(",
    "new Function",
    "fetch(",
    "XMLHttpRequest",
    "WebSocket",
    "require(",
    "process.env",
  ]) {
    assert.equal(appSource.includes(forbidden), false, forbidden);
    assert.equal(mainTsxSource.includes(forbidden), false, forbidden);
  }
  assert.match(appSource, /window\.ravelDesktop/);
  assert.equal(/token/i.test(appSource), false);
  assert.match(indexHtml, /Content-Security-Policy/);
  for (const directive of [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "connect-src 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-src 'none'",
  ]) {
    assert.equal(indexHtml.includes(directive), true, directive);
  }
  assert.equal(/unsafe-inline|unsafe-eval/.test(indexHtml), false);
});

test("the desktop calls only the generated T13 methods it needs", () => {
  const called = new Set(
    [...serviceSource.matchAll(/\.([a-z0-9_]+_v1_[a-z0-9_]+)\(/g)].map((match) => match[1]),
  );
  assert.deepEqual([...called].sort(), [
    "list_projects_v1_projects_get",
    "login_v1_auth_login_post",
    "logout_v1_auth_logout_post",
    "refresh_v1_auth_refresh_post",
  ]);
  assert.match(serviceSource, /from "@ravel\/api-client"/);
  assert.equal(/\bDagRepository\b|dag_mutation|ScientificDag/.test(serviceSource), false);
  assert.equal(/captureException|log.*token|console\.log/.test(serviceSource), false);
});

test("tokens live in memory or in the encrypted session store, never in the renderer", () => {
  assert.match(sessionSource, /safeStorage/);
  assert.match(sessionSource, /encryptString/);
  assert.equal(/writeFileSync\([^)]*accessToken|writeFileSync\([^)]*refreshToken/.test(sessionSource), false);
  assert.equal(/localStorage|sessionStorage/.test(sessionSource), false);
  assert.equal(/["'`]\.env\b|[/\\]\.env\b/.test(mainSource), false);
  assert.equal(/readFileSync\(join\(__dirname, "..", "\.env"/.test(mainSource), false);
  // The renderer receives projections only; the contract has no token field.
  assert.equal(/accessToken|refreshToken/.test(
    fs.readFileSync(path.join(appRoot, "src", "shared", "contract.ts"), "utf8"),
  ), false);
});

test("the Gateway endpoint is operator configuration, not an invented default", () => {
  assert.match(configSource, /RAVEL_GATEWAY_URL/);
  assert.equal(/https:\/\/[a-z0-9.-]+\.(com|ai|io|dev|app)/.test(configSource), false);
  assert.equal(/https:\/\/[a-z0-9.-]+\.(com|ai|io|dev|app)/.test(mainSource), false);
  assert.match(configSource, /must use https \(or http on loopback\)/);
});

test("the desktop dependency pin set is exact and locked", () => {
  assert.equal(manifest.packageManager, undefined);
  const appManifest = JSON.parse(readSource("package.json"));
  assert.equal(appManifest.packageManager, undefined);
  for (const [name, version] of Object.entries(appManifest.devDependencies)) {
    assert.equal(typeof version, "string");
    assert.equal(
      /^[~^<>=*]|latest|workspace:\*$/.test(version) && version !== "workspace:*",
      false,
      `${name}@${version} is not an exact pin`,
    );
  }
  assert.equal(appManifest.devDependencies.electron, "44.9.1");
  assert.equal(appManifest.devDependencies.typescript, "5.6.3");
  assert.equal(appManifest.devDependencies["@ravel/api-client"], "workspace:*");
  const rootManifest = JSON.parse(
    fs.readFileSync(path.join(repoRoot, "package.json"), "utf8"),
  );
  assert.equal(rootManifest.packageManager, "pnpm@9.15.9");
  assert.equal(rootManifest.engines.node, ">=22.12.0 <23");
  assert.equal(fs.existsSync(path.join(repoRoot, "pnpm-lock.yaml")), true);
  const lock = fs.readFileSync(path.join(repoRoot, "pnpm-lock.yaml"), "utf8");
  assert.match(lock, /lockfileVersion: '9\.0'/);
  assert.match(lock, /electron@44\.9\.1/);
});

test("the Windows build is an explicit x64 portable artifact and says it is unsigned", () => {
  const builder = readSource("electron-builder.yml");
  assert.match(builder, /target: portable/);
  assert.match(builder, /- x64/);
  assert.match(builder, /signAndEditExecutable: false/);
  assert.match(builder, /dist\/\*\*\/\*/);
  assert.equal(/publish:/.test(builder), false);
});
