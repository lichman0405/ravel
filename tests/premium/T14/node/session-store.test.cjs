"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");

const { requireDist } = require("./support.cjs");

const storeModule = requireDist("session-store");
const { SessionStore, parseSessionTokens, SESSION_FILE } = storeModule;

function tempDirectory() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "ravel-t14-session-"));
}

/** A fake whose "encryption" is reversible but never equal to the plaintext. */
function fakeSafeStorage(available = true, failure = null) {
  return {
    calls: { encrypt: 0, decrypt: 0 },
    isEncryptionAvailable() {
      return available;
    },
    encryptString(plainText) {
      if (failure) throw failure;
      this.calls.encrypt += 1;
      return Buffer.from(`ravel-test-cipher:${Buffer.from(plainText).toString("base64")}`);
    },
    decryptString(encrypted) {
      this.calls.decrypt += 1;
      const text = encrypted.toString();
      if (!text.startsWith("ravel-test-cipher:")) throw new Error("not our ciphertext");
      return Buffer.from(text.slice("ravel-test-cipher:".length), "base64").toString();
    },
  };
}

const TOKENS = Object.freeze({
  accessToken: "access-token-very-secret",
  refreshToken: "refresh-token-very-secret",
  userId: "user-1",
  username: "owner",
  expiresIn: 3600,
  tokenType: "bearer",
});

test("tokens are written only as ciphertext when encryption exists", () => {
  const directory = tempDirectory();
  const safeStorage = fakeSafeStorage(true);
  const store = new SessionStore(directory, safeStorage);

  assert.equal(store.encryptionAvailable(), true);
  assert.equal(store.save(TOKENS), true);
  assert.equal(store.persistedFileExists(), true);

  const filePath = path.join(directory, SESSION_FILE);
  const raw = fs.readFileSync(filePath, "utf8");
  for (const secret of Object.values(TOKENS)) {
    assert.equal(raw.includes(String(secret)), false, `plaintext ${secret} reached disk`);
  }
  assert.equal(raw.includes("eyJ"), false);
  const envelope = JSON.parse(raw);
  assert.deepEqual(Object.keys(envelope).sort(), ["ciphertext", "format", "version"]);
  assert.equal(envelope.format, "ravel-desktop-session");
  assert.equal(envelope.version, 1);
  assert.equal(typeof envelope.ciphertext, "string");
  assert.equal(raw.includes("accessToken"), false);

  // A fresh process has no in-memory value and must decrypt the file.
  const reopened = new SessionStore(directory, safeStorage);
  const restored = reopened.load();
  assert.deepEqual(restored, { ...TOKENS });
  assert.deepEqual(reopened.current(), { ...TOKENS });
  assert.equal(safeStorage.calls.decrypt, 1);
});

test("an unavailable OS encryption means memory only, never a plaintext file", () => {
  const directory = tempDirectory();
  const safeStorage = fakeSafeStorage(false);
  const store = new SessionStore(directory, safeStorage);

  assert.equal(store.encryptionAvailable(), false);
  assert.equal(store.save(TOKENS), false);
  assert.deepEqual(store.current(), { ...TOKENS });
  assert.equal(fs.existsSync(path.join(directory, SESSION_FILE)), false);
  // The saved value lives only in this process. A new store with no memory
  // finds nothing on disk.
  const reopened = new SessionStore(directory, safeStorage);
  assert.equal(reopened.load(), null);
});

test("a storage that throws is treated as unavailable, not as plaintext", () => {
  const directory = tempDirectory();
  const store = new SessionStore(directory, {
    isEncryptionAvailable() {
      throw new Error("platform unavailable");
    },
    encryptString() {
      throw new Error("must not be called");
    },
    decryptString() {
      throw new Error("must not be called");
    },
  });
  assert.equal(store.encryptionAvailable(), false);
  assert.equal(store.save(TOKENS), false);
  assert.equal(fs.existsSync(path.join(directory, SESSION_FILE)), false);
});

test("a failed encryption writes nothing and leaves no temporary file", () => {
  const directory = tempDirectory();
  const safeStorage = fakeSafeStorage(true, new Error("DPAPI refused"));
  const store = new SessionStore(directory, safeStorage);
  assert.equal(store.save(TOKENS), false);
  assert.equal(fs.existsSync(path.join(directory, SESSION_FILE)), false);
  assert.deepEqual(
    fs.readdirSync(directory).filter((name) => name.includes("session")),
    [],
  );
});

test("clearing forgets memory and removes the encrypted file", () => {
  const directory = tempDirectory();
  const store = new SessionStore(directory, fakeSafeStorage(true));
  store.save(TOKENS);
  assert.equal(store.persistedFileExists(), true);
  store.clear();
  assert.equal(store.current(), null);
  assert.equal(store.load(), null);
  assert.equal(store.persistedFileExists(), false);
});

test("a corrupt or foreign session file is refused", () => {
  const directory = tempDirectory();
  const safeStorage = fakeSafeStorage(true);
  const filePath = path.join(directory, SESSION_FILE);
  const store = new SessionStore(directory, safeStorage);

  fs.writeFileSync(filePath, "{not json");
  assert.equal(store.load(), null);
  fs.writeFileSync(filePath, JSON.stringify({ format: "other", version: 1, ciphertext: "x" }));
  assert.equal(store.load(), null);
  fs.writeFileSync(
    filePath,
    JSON.stringify({ format: "ravel-desktop-session", version: 2, ciphertext: "x" }),
  );
  assert.equal(store.load(), null);
  fs.writeFileSync(
    filePath,
    JSON.stringify({ format: "ravel-desktop-session", version: 1, ciphertext: "plain" }),
  );
  assert.equal(store.load(), null);
  assert.equal(store.current(), null);
});

test("a decrypted payload is validated before it becomes a session", () => {
  assert.deepEqual(parseSessionTokens(TOKENS), { ...TOKENS });
  assert.deepEqual(parseSessionTokens({ ...TOKENS, tokenType: undefined }), {
    ...TOKENS,
    tokenType: "bearer",
  });
  for (const broken of [
    null,
    "tokens",
    {},
    { ...TOKENS, accessToken: "" },
    { ...TOKENS, refreshToken: 7 },
    { ...TOKENS, userId: "" },
    { ...TOKENS, username: "" },
    { ...TOKENS, expiresIn: 0 },
    { ...TOKENS, expiresIn: -1 },
    { ...TOKENS, expiresIn: "3600" },
    { ...TOKENS, expiresIn: Number.POSITIVE_INFINITY },
  ]) {
    assert.equal(parseSessionTokens(broken), null, JSON.stringify(broken));
  }
});
