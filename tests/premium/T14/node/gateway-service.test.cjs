"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");

const { requireDist } = require("./support.cjs");

const { GatewayError, GatewayService } = requireDist("gateway-service");

const BASE_URL = "http://127.0.0.1:8123";

const PAIR = Object.freeze({
  access_token: "access-token-1",
  refresh_token: "refresh-token-1",
  token_type: "bearer",
  expires_in: 3600,
  user_id: "user-1",
  username: "owner",
});

function pair(n) {
  return {
    access_token: `access-token-${n}`,
    refresh_token: `refresh-token-${n}`,
    token_type: "bearer",
    expires_in: 3600,
    user_id: "user-1",
    username: "owner",
  };
}

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function apiError(status, code) {
  return json(status, {
    code,
    message: "refused",
    request_id: "request-1",
    details: {},
  });
}

class FakeGateway {
  constructor(handler) {
    this.handler = handler;
    this.requests = [];
  }

  get fetch() {
    return async (url, init) => {
      const parsed = new URL(url);
      const request = {
        url,
        path: parsed.pathname,
        query: parsed.searchParams,
        method: init.method,
        headers: init.headers,
        body: init.body === undefined ? undefined : JSON.parse(init.body),
        signal: init.signal,
      };
      this.requests.push(request);
      return this.handler(request);
    };
  }

  paths() {
    return this.requests.map((request) => request.path);
  }
}

function memoryStore(initial = null) {
  return {
    tokens: initial,
    saved: [],
    clears: 0,
    save(tokens) {
      this.saved.push(tokens);
      this.tokens = tokens;
      return true;
    },
    load() {
      return this.tokens;
    },
    clear() {
      this.clears += 1;
      this.tokens = null;
    },
  };
}

function service(fetchImpl, store = memoryStore(), timeoutMs = 250) {
  return new GatewayService({
    baseUrl: BASE_URL,
    configurationProblem: null,
    store,
    fetchImpl,
    timeoutMs,
  });
}

async function signedIn(handler, store = memoryStore()) {
  const server = new FakeGateway(handler);
  const instance = service(server.fetch, store);
  await instance.login({ username: "owner", password: "correct horse" });
  return { server, instance, store };
}

test("login uses the Gateway's real endpoint and projects a session without tokens", async () => {
  const server = new FakeGateway((request) =>
    request.path === "/v1/auth/login" ? json(200, PAIR) : apiError(404, "NOT_FOUND"),
  );
  const store = memoryStore();
  const instance = service(server.fetch, store);

  const session = await instance.login({ username: " owner ", password: "correct horse" });

  assert.deepEqual(session, {
    authenticated: true,
    user: { userId: "user-1", username: "owner" },
    configurationRequired: false,
    gatewayOrigin: BASE_URL,
  });
  assert.equal(JSON.stringify(session).includes("access-token"), false);
  assert.equal(JSON.stringify(session).includes("refresh-token"), false);
  assert.deepEqual(server.requests[0].body, {
    username: "owner",
    password: "correct horse",
  });
  assert.equal(server.requests[0].method, "POST");
  assert.equal(store.saved.length, 1);
  assert.equal(store.saved[0].accessToken, "access-token-1");
  assert.equal(instance.connection().state, "ONLINE");
});

test("a refused password is a credential error, not an offline Gateway", async () => {
  const server = new FakeGateway(() => apiError(401, "INVALID_CREDENTIALS"));
  const store = memoryStore();
  const instance = service(server.fetch, store);

  await assert.rejects(
    instance.login({ username: "owner", password: "wrong" }),
    (error) =>
      error instanceof GatewayError &&
      error.code === "INVALID_CREDENTIALS" &&
      !error.message.includes("wrong"),
  );
  assert.equal(instance.session().authenticated, false);
  assert.equal(instance.connection().state, "ONLINE");
  assert.equal(store.saved.length, 0);
});

test("an unreachable Gateway becomes OFFLINE and keeps no credentials", async () => {
  const server = new FakeGateway(() => {
    throw new TypeError("fetch failed");
  });
  const store = memoryStore();
  const instance = service(server.fetch, store);

  await assert.rejects(
    instance.login({ username: "owner", password: "correct horse" }),
    (error) => error instanceof GatewayError && error.code === "GATEWAY_OFFLINE",
  );
  assert.equal(instance.connection().state, "OFFLINE");
  assert.equal(instance.session().authenticated, false);
  assert.equal(store.saved.length, 0);
});

test("the project list is read with the access token and only display fields survive", async () => {
  const { server, instance } = await signedIn((request) => {
    if (request.path === "/v1/auth/login") return json(200, PAIR);
    if (request.path === "/v1/projects") {
      return json(200, {
        items: [
          {
            display_id: "P-001",
            project_id: "project-1",
            title: "Catalyst screen",
            objective: "Find a better dopant.",
            status: "RUNNING",
            role: "PROJECT_OWNER",
            internal_note: "must not reach the renderer",
          },
        ],
        has_more: true,
        next_cursor: "next-page",
        internal_page: "must not reach the renderer",
      });
    }
    return apiError(404, "NOT_FOUND");
  });

  const page = await instance.listProjects("cursor-1");

  assert.deepEqual(page, {
    items: [
      {
        display_id: "P-001",
        project_id: "project-1",
        title: "Catalyst screen",
        objective: "Find a better dopant.",
        status: "RUNNING",
        role: "PROJECT_OWNER",
      },
    ],
    has_more: true,
    next_cursor: "next-page",
  });
  const read = server.requests.find((request) => request.path === "/v1/projects");
  assert.equal(read.method, "GET");
  assert.equal(read.headers.Authorization, "Bearer access-token-1");
  assert.equal(read.query.get("cursor"), "cursor-1");
  assert.equal(JSON.stringify(page).includes("access-token"), false);
  assert.equal(JSON.stringify(page).includes("must not reach the renderer"), false);
  assert.equal(instance.connection().state, "ONLINE");
});

test("a concurrent 401 rotates the refresh chain exactly once", async () => {
  let projectReads = 0;
  const { server, instance, store } = await signedIn(async (request) => {
    if (request.path === "/v1/auth/login") return json(200, PAIR);
    if (request.path === "/v1/auth/refresh") {
      await new Promise((resolve) => setTimeout(resolve, 15));
      assert.deepEqual(request.body, { refresh_token: "refresh-token-1" });
      return json(200, pair(2));
    }
    if (request.path === "/v1/projects") {
      projectReads += 1;
      await new Promise((resolve) => setTimeout(resolve, 5));
      if (request.headers.Authorization === "Bearer access-token-1") {
        return apiError(401, "AUTH_REQUIRED");
      }
      return json(200, { items: [], has_more: false });
    }
    return apiError(404, "NOT_FOUND");
  });

  const [first, second] = await Promise.all([
    instance.listProjects(),
    instance.listProjects(),
  ]);

  assert.deepEqual(first.items, []);
  assert.deepEqual(second.items, []);
  assert.equal(server.paths().filter((path) => path === "/v1/auth/refresh").length, 1);
  assert.equal(projectReads, 4);
  assert.equal(store.saved.at(-1).accessToken, "access-token-2");
  assert.equal(store.saved.at(-1).refreshToken, "refresh-token-2");
});

test("a replayed or expired refresh token signs the desktop out", async () => {
  const store = memoryStore();
  const { instance } = await signedIn((request) => {
    if (request.path === "/v1/auth/login") return json(200, PAIR);
    if (request.path === "/v1/auth/refresh") return apiError(401, "AUTH_REQUIRED");
    return apiError(401, "AUTH_REQUIRED");
  }, store);

  await assert.rejects(
    instance.listProjects(),
    (error) => error instanceof GatewayError && error.code === "AUTH_REQUIRED",
  );
  assert.equal(instance.session().authenticated, false);
  assert.equal(instance.connection().state, "SIGNED_OUT");
  assert.equal(store.clears, 1);
});

test("a project read refused after a successful refresh is reported, not retried forever", async () => {
  let projectReads = 0;
  const { server, instance } = await signedIn((request) => {
    if (request.path === "/v1/auth/login") return json(200, PAIR);
    if (request.path === "/v1/auth/refresh") return json(200, pair(2));
    if (request.path === "/v1/projects") {
      projectReads += 1;
      return projectReads === 1 ? apiError(401, "AUTH_REQUIRED") : apiError(403, "FORBIDDEN");
    }
    return apiError(404, "NOT_FOUND");
  });

  await assert.rejects(
    instance.listProjects(),
    (error) => error instanceof GatewayError && error.code === "GATEWAY_ERROR",
  );
  assert.equal(server.paths().filter((path) => path === "/v1/projects").length, 2);
  assert.equal(instance.connection().state, "ONLINE");
});

test("logout tells the Gateway, forgets locally, and exposes no further read", async () => {
  const { server, instance, store } = await signedIn((request) => {
    if (request.path === "/v1/auth/login") return json(200, PAIR);
    if (request.path === "/v1/auth/logout") {
      return new Response(null, { status: 204 });
    }
    return apiError(404, "NOT_FOUND");
  });

  const session = await instance.logout();

  assert.equal(session.authenticated, false);
  assert.equal(instance.connection().state, "SIGNED_OUT");
  assert.equal(store.tokens, null);
  assert.equal(store.clears, 1);
  const logout = server.requests.find((request) => request.path === "/v1/auth/logout");
  assert.deepEqual(logout.body, { refresh_token: "refresh-token-1" });
  await assert.rejects(
    instance.listProjects(),
    (error) => error instanceof GatewayError && error.code === "AUTH_REQUIRED",
  );
});

test("logout still clears locally when the Gateway cannot be reached", async () => {
  let offline = false;
  const { instance, store } = await signedIn((request) => {
    if (offline) throw new TypeError("fetch failed");
    return json(200, PAIR);
  });

  offline = true;
  const session = await instance.logout();
  assert.equal(session.authenticated, false);
  assert.equal(store.tokens, null);
  assert.equal(instance.connection().state, "SIGNED_OUT");
});

test("restore without a saved session signs out and makes no request", async () => {
  const server = new FakeGateway(() => {
    throw new Error("must not be called");
  });
  const instance = service(server.fetch, memoryStore(null));
  const session = await instance.restore();
  assert.equal(session.authenticated, false);
  assert.equal(instance.connection().state, "SIGNED_OUT");
  assert.equal(server.requests.length, 0);
});

test("restore rotates a saved refresh token into a working session", async () => {
  const store = memoryStore({
    accessToken: "old-access",
    refreshToken: "refresh-token-1",
    userId: "user-1",
    username: "owner",
    expiresIn: 3600,
    tokenType: "bearer",
  });
  const server = new FakeGateway((request) => {
    if (request.path === "/v1/auth/refresh") return json(200, pair(2));
    return apiError(404, "NOT_FOUND");
  });
  const instance = service(server.fetch, store);

  const session = await instance.restore();
  assert.equal(session.authenticated, true);
  assert.equal(instance.connection().state, "ONLINE");
  assert.equal(store.saved.at(-1).refreshToken, "refresh-token-2");
});

test("offline restore keeps the saved pair, reports OFFLINE and reconnects", async () => {
  const store = memoryStore({
    accessToken: "old-access",
    refreshToken: "refresh-token-1",
    userId: "user-1",
    username: "owner",
    expiresIn: 3600,
    tokenType: "bearer",
  });
  let online = false;
  const server = new FakeGateway((request) => {
    if (!online) throw new TypeError("fetch failed");
    if (request.path === "/v1/auth/refresh") return json(200, pair(2));
    if (request.path === "/v1/projects") return json(200, { items: [] });
    return apiError(404, "NOT_FOUND");
  });
  const instance = service(server.fetch, store);

  const restored = await instance.restore();
  assert.equal(restored.authenticated, true);
  assert.equal(instance.connection().state, "OFFLINE");

  online = true;
  const status = await instance.reconnect();
  assert.equal(status.state, "ONLINE");
  assert.deepEqual((await instance.listProjects()).items, []);
});

test("restore with a refused refresh token signs out and clears the store", async () => {
  const store = memoryStore({
    accessToken: "old-access",
    refreshToken: "revoked",
    userId: "user-1",
    username: "owner",
    expiresIn: 3600,
    tokenType: "bearer",
  });
  const server = new FakeGateway(() => apiError(401, "AUTH_REQUIRED"));
  const instance = service(server.fetch, store);

  const session = await instance.restore();
  assert.equal(session.authenticated, false);
  assert.equal(instance.connection().state, "SIGNED_OUT");
  assert.equal(store.clears, 1);
});

test("reconnect never throws and reports the real state", async () => {
  const server = new FakeGateway(() => {
    throw new TypeError("fetch failed");
  });
  const store = memoryStore(null);
  const instance = service(server.fetch, store);

  assert.equal((await instance.reconnect()).state, "SIGNED_OUT");
  assert.equal(server.requests.length, 0);

  store.tokens = {
    accessToken: "old-access",
    refreshToken: "refresh-token-1",
    userId: "user-1",
    username: "owner",
    expiresIn: 3600,
    tokenType: "bearer",
  };
  const status = await instance.reconnect();
  assert.equal(status.state, "OFFLINE");
});

test("an unconfigured desktop refuses Gateway calls and states the configuration gap", async () => {
  const store = memoryStore();
  const instance = new GatewayService({
    baseUrl: null,
    configurationProblem: "No Gateway is configured. Set RAVEL_GATEWAY_URL.",
    store,
    fetchImpl: async () => {
      throw new Error("must not be called");
    },
  });

  assert.deepEqual(instance.session(), {
    authenticated: false,
    user: null,
    configurationRequired: true,
    gatewayOrigin: null,
  });
  assert.equal(instance.connection().state, "CONFIGURATION_REQUIRED");
  assert.match(instance.connection().detail, /No Gateway is configured/);
  await assert.rejects(
    instance.login({ username: "owner", password: "pw" }),
    (error) => error instanceof GatewayError && error.code === "CONFIGURATION_REQUIRED",
  );
  assert.equal((await instance.reconnect()).state, "CONFIGURATION_REQUIRED");
});

test("a Gateway that never answers becomes OFFLINE within the configured timeout", async () => {
  const hangingFetch = (_url, init) =>
    new Promise((_resolve, reject) => {
      // A real fetch owns a socket handle; hold the event loop open so this
      // test measures the service's timeout rather than Node exiting.
      const keepAlive = setTimeout(() => reject(new Error("the fake never answered")), 5000);
      init.signal.addEventListener("abort", () => {
        clearTimeout(keepAlive);
        const error = new Error("aborted");
        error.name = "AbortError";
        reject(error);
      });
    });
  const instance = new GatewayService({
    baseUrl: BASE_URL,
    configurationProblem: null,
    store: memoryStore(),
    fetchImpl: hangingFetch,
    timeoutMs: 20,
  });

  const started = Date.now();
  await assert.rejects(
    instance.login({ username: "owner", password: "pw" }),
    (error) => error instanceof GatewayError && error.code === "GATEWAY_OFFLINE",
  );
  assert.ok(Date.now() - started < 1000);
  assert.equal(instance.connection().state, "OFFLINE");
});

test("the service has no scientific-DAG mutation surface", () => {
  const names = Object.getOwnPropertyNames(GatewayService.prototype);
  for (const name of names) {
    assert.equal(
      /dag|approve|reject|mutate|create|update|delete|pause|resume|contract|evidence|lab|mail/i.test(
        name,
      ),
      false,
      `unexpected mutating method ${name}`,
    );
  }
  assert.deepEqual(
    names.filter((name) => !name.startsWith("_") && name !== "constructor").sort(),
    [
      "client",
      "connection",
      "forget",
      "listProjects",
      "login",
      "logout",
      "makeStatus",
      "offline",
      "performRefresh",
      "reconnect",
      "refresh",
      "refused",
      "restore",
      "session",
      "setStatus",
      "signal",
    ].sort(),
  );
});
