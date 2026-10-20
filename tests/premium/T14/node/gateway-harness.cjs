#!/usr/bin/env node
/**
 * Exercise the desktop's real main-process service against a real Gateway.
 *
 * This is not a renderer and not a second API client: it constructs
 * GatewayService and SessionStore from the built desktop bundle, the same
 * classes Electron's main process uses, and talks to a Gateway over a real
 * socket. The session directory and a deterministic test cipher stand in for
 * DPAPI so the harness can run in a Linux gate container; the Windows Electron
 * test exercises the real safeStorage path.
 *
 * Phases let the Python case stop the Gateway between processes:
 *
 *   login     wrong password, then real sign-in, project list, encrypted file
 *   offline   restart the service while the Gateway is down
 *   restored  restart after the Gateway returns, rotate, list, sign out
 *
 * Only projections, booleans and hashes leave this process. Raw tokens stay in
 * this process's memory and are never printed.
 */

"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const { GatewayService, GatewayError } = require(
  path.resolve(__dirname, "..", "..", "..", "..", "apps", "desktop", "dist", "lib", "gateway-service.cjs"),
);
const { SessionStore, SESSION_FILE } = require(
  path.resolve(__dirname, "..", "..", "..", "..", "apps", "desktop", "dist", "lib", "session-store.cjs"),
);

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key || !key.startsWith("--") || value === undefined) {
      throw new Error("arguments are --name value pairs");
    }
    values[key.slice(2)] = value;
  }
  return values;
}

function testSafeStorage() {
  const key = Buffer.from("ravel-t14-gate-harness");
  return {
    isEncryptionAvailable() {
      return true;
    },
    encryptString(plainText) {
      const data = Buffer.from(plainText, "utf8");
      const out = Buffer.alloc(data.length);
      for (let index = 0; index < data.length; index += 1) {
        out[index] = data[index] ^ key[index % key.length];
      }
      return Buffer.concat([Buffer.from("t14harness:"), out]);
    },
    decryptString(encrypted) {
      const prefix = "t14harness:";
      const text = encrypted.toString("latin1");
      if (!text.startsWith(prefix)) throw new Error("not harness ciphertext");
      const data = Buffer.from(text.slice(prefix.length), "latin1");
      const out = Buffer.alloc(data.length);
      for (let index = 0; index < data.length; index += 1) {
        out[index] = data[index] ^ key[index % key.length];
      }
      return out.toString("utf8");
    },
  };
}

function fingerprint(token) {
  return crypto.createHash("sha256").update(token).digest("hex").slice(0, 16);
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function baseOptions(args, store) {
  return {
    baseUrl: args["base-url"],
    configurationProblem: null,
    store,
    timeoutMs: 4000,
  };
}

async function login(args, store) {
  const service = new GatewayService(baseOptions(args, store));

  let wrongPassword;
  try {
    await service.login({ username: args.username, password: `${args.password}-wrong` });
    wrongPassword = "accepted";
  } catch (error) {
    wrongPassword =
      error instanceof GatewayError ? error.code : "unexpected-error";
  }
  assert(wrongPassword === "INVALID_CREDENTIALS", `wrong password was ${wrongPassword}`);
  assert(service.session().authenticated === false, "wrong password created a session");

  const session = await service.login({
    username: args.username,
    password: args.password,
  });
  assert(session.authenticated === true, "login did not authenticate");
  assert(session.configurationRequired === false, "login reported missing configuration");
  assert(session.user.username === args.username, "login projected the wrong user");

  const page = await service.listProjects();
  const titles = page.items.map((project) => project.title);
  assert(titles.includes(args.project), `project list did not include ${args.project}`);
  const chosen = page.items.find((project) => project.title === args.project);
  assert(chosen.display_id.startsWith("P-"), "project has no display id");
  assert(chosen.role === "PROJECT_OWNER", `project role is ${chosen.role}`);

  const tokens = store.current();
  assert(tokens !== null, "session store has no in-memory tokens");
  const raw = fs.readFileSync(path.join(args["session-dir"], SESSION_FILE), "utf8");
  const envelope = JSON.parse(raw);
  assert(envelope.format === "ravel-desktop-session", "session envelope format changed");
  assert(envelope.version === 1, "session envelope version changed");
  assert(typeof envelope.ciphertext === "string", "session envelope has no ciphertext");
  for (const secret of [tokens.accessToken, tokens.refreshToken]) {
    assert(raw.includes(secret) === false, "a token reached the session file in plaintext");
  }
  assert(raw.includes('"accessToken"') === false, "token field names reached the session file");

  const result = {
    phase: "login",
    wrong_password: wrongPassword,
    authenticated: session.authenticated,
    user: { username: session.user.username },
    gateway_origin: session.gatewayOrigin,
    project: {
      display_id: chosen.display_id,
      title: chosen.title,
      status: chosen.status,
      role: chosen.role,
    },
    project_count: page.items.length,
    access_fingerprint: fingerprint(tokens.accessToken),
    refresh_fingerprint: fingerprint(tokens.refreshToken),
    session_file: {
      exists: true,
      envelope_keys: Object.keys(envelope).sort(),
      contains_plaintext_token: false,
      size: Buffer.byteLength(raw),
    },
    projection_keys: Object.keys(session).sort(),
  };
  fs.writeFileSync(
    path.join(args["session-dir"], "handoff.json"),
    JSON.stringify({ refresh_fingerprint: result.refresh_fingerprint }),
    { mode: 0o600 },
  );
  return result;
}

async function offline(args, store) {
  const service = new GatewayService(baseOptions(args, store));
  const restored = await service.restore();
  const status = service.connection();
  assert(status.state === "OFFLINE", `offline restore reported ${status.state}`);
  assert(restored.authenticated === true, "offline restore forgot the saved session");

  let listed;
  try {
    await service.listProjects();
    listed = "accepted";
  } catch (error) {
    listed = error instanceof GatewayError ? error.code : "unexpected-error";
  }
  assert(listed === "GATEWAY_OFFLINE", `offline project read was ${listed}`);

  const reconnected = await service.reconnect();
  assert(reconnected.state === "OFFLINE", `reconnect while down reported ${reconnected.state}`);
  return {
    phase: "offline",
    restored_authenticated: restored.authenticated,
    status: restored ? status.state : "missing",
    project_read: listed,
    reconnect_state: reconnected.state,
    detail_is_displayable: typeof status.detail === "string",
  };
}

async function restored(args, store) {
  const service = new GatewayService(baseOptions(args, store));
  const session = await service.restore();
  assert(session.authenticated === true, "restored service is not authenticated");
  assert(service.connection().state === "ONLINE", "restored service is not online");

  const page = await service.listProjects();
  assert(
    page.items.some((project) => project.title === args.project),
    "restored project list lost the project",
  );

  const handoff = JSON.parse(
    fs.readFileSync(path.join(args["session-dir"], "handoff.json"), "utf8"),
  );
  const tokens = store.current();
  assert(tokens !== null, "rotated session is missing");
  assert(
    fingerprint(tokens.refreshToken) !== handoff.refresh_fingerprint,
    "restore did not rotate the refresh token",
  );

  const refreshBeforeLogout = tokens.refreshToken;
  const logout = await service.logout();
  assert(logout.authenticated === false, "logout left an authenticated session");
  assert(fs.existsSync(path.join(args["session-dir"], SESSION_FILE)) === false,
    "logout left the session file");

  let revocation;
  try {
    const response = await fetch(`${args["base-url"]}/v1/auth/refresh`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshBeforeLogout }),
    });
    revocation = response.status;
  } catch {
    revocation = "unreachable";
  }
  assert(revocation === 401, `revoked refresh token was answered with ${revocation}`);

  return {
    phase: "restored",
    authenticated_after_restart: session.authenticated,
    status_after_restart: "ONLINE",
    refresh_rotated: true,
    projects_visible: page.items.length,
    logout_authenticated: logout.authenticated,
    session_file_after_logout: false,
    revoked_refresh_status: revocation,
  };
}

async function scope(args, store) {
  const service = new GatewayService(baseOptions(args, store));
  const session = await service.login({
    username: args.username,
    password: args.password,
  });
  assert(session.authenticated === true, "scope login did not authenticate");
  const page = await service.listProjects();
  return {
    phase: "scope",
    authenticated: session.authenticated,
    project_count: page.items.length,
    titles: page.items.map((project) => project.title),
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  for (const required of ["phase", "base-url", "username", "password", "session-dir"]) {
    assert(typeof args[required] === "string" && args[required] !== "", `missing --${required}`);
  }
  if (args.phase !== "scope") {
    assert(typeof args.project === "string" && args.project !== "", "missing --project");
  }
  fs.mkdirSync(args["session-dir"], { recursive: true, mode: 0o700 });
  const store = new SessionStore(args["session-dir"], testSafeStorage());
  let result;
  if (args.phase === "login") result = await login(args, store);
  else if (args.phase === "offline") result = await offline(args, store);
  else if (args.phase === "restored") result = await restored(args, store);
  else if (args.phase === "scope") result = await scope(args, store);
  else throw new Error(`unknown phase ${args.phase}`);

  const rendered = JSON.stringify(result);
  const tokens = store.current();
  if (tokens !== null) {
    for (const secret of [tokens.accessToken, tokens.refreshToken]) {
      assert(rendered.includes(secret) === false, "a token would have been printed");
    }
  }
  process.stdout.write(`${rendered}\n`);
}

main().catch((error) => {
  process.stderr.write(
    `${error instanceof Error ? error.message : "harness failed"}\n`,
  );
  process.exit(1);
});
