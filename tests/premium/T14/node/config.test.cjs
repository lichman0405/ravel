"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");

const { requireDist } = require("./support.cjs");

const config = requireDist("config");

function memoryFs(files, failure) {
  return {
    readFileSync(file) {
      if (failure) throw failure;
      if (Object.hasOwn(files, file)) return files[file];
      const error = new Error("missing");
      error.code = "ENOENT";
      throw error;
    },
  };
}

const USER_DATA = "C:/Users/ravel/AppData/Roaming/RAVEL Desktop";

test("an https Gateway is accepted and normalised", () => {
  assert.equal(
    config.validateGatewayUrl(" https://gateway.ravel.example/ "),
    "https://gateway.ravel.example",
  );
  assert.equal(
    config.validateGatewayUrl("https://gateway.ravel.example/base///"),
    "https://gateway.ravel.example/base",
  );
  assert.equal(
    config.validateGatewayUrl("HTTPS://Gateway.Ravel.Example"),
    "https://gateway.ravel.example",
  );
});

test("plain http is admitted only on loopback", () => {
  for (const url of [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://[::1]:8000",
  ]) {
    assert.equal(config.validateGatewayUrl(url).startsWith("http://"), true, url);
  }
  assert.throws(
    () => config.validateGatewayUrl("http://gateway.ravel.example"),
    /plain http is allowed only for a loopback Gateway/,
  );
  assert.throws(
    () => config.validateGatewayUrl("http://192.168.1.10:8000"),
    /loopback/,
  );
});

test("a Gateway URL cannot smuggle credentials, a query or a fragment", () => {
  assert.throws(
    () => config.validateGatewayUrl("https://user:secret@gateway.ravel.example"),
    /must not contain credentials/,
  );
  assert.throws(
    () => config.validateGatewayUrl("https://gateway.ravel.example/?token=abc"),
    /must not contain a query or fragment/,
  );
  assert.throws(
    () => config.validateGatewayUrl("https://gateway.ravel.example/#token"),
    /must not contain a query or fragment/,
  );
  assert.throws(() => config.validateGatewayUrl("file:///etc/passwd"), /must use https/);
  assert.throws(() => config.validateGatewayUrl(""), /empty/);
  assert.throws(() => config.validateGatewayUrl("not a url"), /not a valid URL/);
});

test("the environment endpoint wins over the configuration file", () => {
  const resolved = config.resolveGatewayConfig({
    env: { RAVEL_GATEWAY_URL: "https://env.ravel.example" },
    userDataDir: USER_DATA,
    fs: memoryFs({
      [`${USER_DATA}/gateway.json`]: JSON.stringify({
        gatewayUrl: "https://file.ravel.example",
      }),
    }),
  });
  assert.deepEqual(resolved, { baseUrl: "https://env.ravel.example", problem: null });
});

test("an invalid environment endpoint is refused without falling back", () => {
  const resolved = config.resolveGatewayConfig({
    env: { RAVEL_GATEWAY_URL: "http://remote.ravel.example" },
    userDataDir: USER_DATA,
    fs: memoryFs({
      [`${USER_DATA}/gateway.json`]: JSON.stringify({
        gatewayUrl: "https://file.ravel.example",
      }),
    }),
  });
  assert.equal(resolved.baseUrl, null);
  assert.match(resolved.problem, /^RAVEL_GATEWAY_URL: plain http/);
});

test("the configuration file is read only when the environment is silent", () => {
  const resolved = config.resolveGatewayConfig({
    env: {},
    userDataDir: USER_DATA,
    fs: memoryFs({
      [`${USER_DATA}/gateway.json`]: JSON.stringify({
        gatewayUrl: "https://file.ravel.example/",
      }),
    }),
  });
  assert.deepEqual(resolved, { baseUrl: "https://file.ravel.example", problem: null });
});

test("an unconfigured desktop says so instead of guessing an endpoint", () => {
  const resolved = config.resolveGatewayConfig({
    env: {},
    userDataDir: USER_DATA,
    fs: memoryFs({}),
  });
  assert.equal(resolved.baseUrl, null);
  assert.match(resolved.problem, /No Gateway is configured/);
  assert.match(resolved.problem, /RAVEL_GATEWAY_URL/);
  assert.match(resolved.problem, /gateway\.json/);
});

test("a broken configuration file becomes a stated problem, not an endpoint", () => {
  for (const [body, pattern] of [
    ["{not json", /not valid JSON/],
    ["[]", /must be an object/],
    ["{}", /must contain a gatewayUrl string/],
    [JSON.stringify({ gatewayUrl: 42 }), /must contain a gatewayUrl string/],
    [JSON.stringify({ gatewayUrl: "http://remote.test" }), /gateway\.json: plain http/],
  ]) {
    const resolved = config.resolveGatewayConfig({
      env: {},
      userDataDir: USER_DATA,
      fs: memoryFs({ [`${USER_DATA}/gateway.json`]: body }),
    });
    assert.equal(resolved.baseUrl, null, body);
    assert.match(resolved.problem, pattern, body);
  }
});

test("an unreadable configuration file is not treated as absent", () => {
  const denied = new Error("denied");
  denied.code = "EACCES";
  const resolved = config.resolveGatewayConfig({
    env: {},
    userDataDir: USER_DATA,
    fs: memoryFs({}, denied),
  });
  assert.equal(resolved.baseUrl, null);
  assert.match(resolved.problem, /could not be read/);
});
