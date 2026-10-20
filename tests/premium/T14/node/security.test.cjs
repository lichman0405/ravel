"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");

const { requireDist } = require("./support.cjs");

const security = requireDist("security");

function frame(url, parent = null) {
  return { url, parent };
}

function contents(id = 1) {
  const mainFrame = frame("file:///opt/ravel/dist/renderer/index.html");
  return {
    id,
    isDestroyed: () => false,
    mainFrame,
  };
}

const RENDERER = "file:///opt/ravel/dist/renderer/index.html";

test("the only window preferences are the hardened ones", () => {
  const preferences = security.secureWebPreferences("/opt/ravel/dist/main/preload.cjs");
  assert.deepEqual(preferences, {
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
    preload: "/opt/ravel/dist/main/preload.cjs",
  });
  assert.equal(Object.isFrozen(preferences), true);
});

test("the trusted renderer URL is exact, and an anchor is the same document", () => {
  assert.equal(security.isTrustedRendererUrl(RENDERER, RENDERER), true);
  assert.equal(security.isTrustedRendererUrl(`${RENDERER}#projects`, RENDERER), true);
  assert.equal(
    security.isTrustedRendererUrl("file:///opt/ravel/dist/renderer/other.html", RENDERER),
    false,
  );
  assert.equal(
    security.isTrustedRendererUrl(`${RENDERER}?next=https://evil.test`, RENDERER),
    false,
  );
  assert.equal(
    security.isTrustedRendererUrl("https://gateway.evil.test/", RENDERER),
    false,
  );
  assert.equal(security.isTrustedRendererUrl("data:text/html,<h1>x", RENDERER), false);
  assert.equal(security.isTrustedRendererUrl("about:blank", RENDERER), false);
  assert.equal(security.isTrustedRendererUrl("not a url", RENDERER), false);
});

test("navigation is allowed only to the exact renderer document", () => {
  assert.equal(security.navigationAllowed(RENDERER, RENDERER), true);
  assert.equal(
    security.navigationAllowed("https://attacker.test/login", RENDERER),
    false,
  );
  assert.equal(
    security.navigationAllowed("file:///opt/ravel/.env", RENDERER),
    false,
  );
});

test("an IPC sender must be the live main frame of the main window", () => {
  const sender = contents();
  const valid = { sender, senderFrame: sender.mainFrame };
  assert.equal(security.isTrustedIpcSender(valid, sender, RENDERER), true);

  assert.equal(security.isTrustedIpcSender(valid, null, RENDERER), false);

  const other = contents(2);
  const otherFrame = frame(RENDERER);
  other.mainFrame = otherFrame;
  assert.equal(
    security.isTrustedIpcSender({ sender: other, senderFrame: otherFrame }, sender, RENDERER),
    false,
  );

  const destroyed = contents(3);
  destroyed.isDestroyed = () => true;
  assert.equal(
    security.isTrustedIpcSender(
      { sender: destroyed, senderFrame: frame(RENDERER) },
      destroyed,
      RENDERER,
    ),
    false,
  );

  assert.equal(security.isTrustedIpcSender({ sender, senderFrame: null }, sender, RENDERER), false);

  const child = frame(RENDERER, frame(RENDERER));
  assert.equal(
    security.isTrustedIpcSender({ sender, senderFrame: child }, sender, RENDERER),
    false,
  );

  const foreign = frame("https://attacker.test/");
  assert.equal(
    security.isTrustedIpcSender({ sender, senderFrame: foreign }, sender, RENDERER),
    false,
  );

  // Same URL but not the frame object Electron says is the main frame.
  const impostor = frame(RENDERER);
  assert.equal(
    security.isTrustedIpcSender({ sender, senderFrame: impostor }, sender, RENDERER),
    false,
  );
});

test("no window may be opened and no web permission is granted", () => {
  assert.deepEqual(security.windowOpenDecision(), { action: "deny" });
  assert.equal(security.permissionRequestAllowed(), false);
});
