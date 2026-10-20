/**
 * Token persistence with the operating system's encryption, or not at all.
 *
 * Electron's `safeStorage` is backed by DPAPI on Windows, the Keychain on
 * macOS and libsecret on Linux. When it is unavailable (an unusual Linux
 * session, for example) the only alternative is memory: this store never falls
 * back to plaintext. A file on disk contains only base64 ciphertext and a
 * format marker.
 *
 * The store holds the refresh token as well as the access token because the
 * refresh token is what makes a restart restore a session. Neither value is
 * ever returned to the renderer; the main process reads them itself.
 */

import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";

export const SESSION_FILE = "session.dat";
const FORMAT = "ravel-desktop-session";
const VERSION = 1;

export interface SafeStorageLike {
  isEncryptionAvailable(): boolean;
  encryptString(plainText: string): Buffer;
  decryptString(encrypted: Buffer): string;
}

export interface SessionTokens {
  readonly accessToken: string;
  readonly refreshToken: string;
  readonly userId: string;
  readonly username: string;
  readonly expiresIn: number;
  readonly tokenType: string;
}

export class SessionStoreError extends Error {
  constructor(readonly code: "READ_FAILED" | "WRITE_FAILED" | "DECRYPT_FAILED") {
    // Codes only. A storage error message can contain a path but must never
    // contain token material, and the caller has nothing useful to do with the
    // ciphertext that failed.
    super(`session store ${code.toLowerCase()}`);
    this.name = "SessionStoreError";
  }
}

function asNonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

/** Validate a decrypted object before it becomes an authenticated session. */
export function parseSessionTokens(value: unknown): SessionTokens | null {
  if (value === null || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const accessToken = asNonEmptyString(record.accessToken);
  const refreshToken = asNonEmptyString(record.refreshToken);
  const userId = asNonEmptyString(record.userId);
  const username = asNonEmptyString(record.username);
  const tokenType = asNonEmptyString(record.tokenType) ?? "bearer";
  const expiresIn = record.expiresIn;
  if (
    accessToken === null ||
    refreshToken === null ||
    userId === null ||
    username === null ||
    typeof expiresIn !== "number" ||
    !Number.isFinite(expiresIn) ||
    expiresIn <= 0
  ) {
    return null;
  }
  return { accessToken, refreshToken, userId, username, expiresIn, tokenType };
}

export class SessionStore {
  private cached: SessionTokens | null = null;

  constructor(
    private readonly directory: string,
    private readonly safeStorage: SafeStorageLike,
  ) {}

  private get filePath(): string {
    return join(this.directory, SESSION_FILE);
  }

  encryptionAvailable(): boolean {
    try {
      return this.safeStorage.isEncryptionAvailable();
    } catch {
      return false;
    }
  }

  /** The in-memory session, if this process has one. Never reads disk. */
  current(): SessionTokens | null {
    return this.cached;
  }

  hasPersistedSession(): boolean {
    return existsSync(this.filePath);
  }

  load(): SessionTokens | null {
    if (this.cached !== null) return this.cached;
    if (!this.encryptionAvailable()) return null;
    let envelope: unknown;
    try {
      envelope = JSON.parse(readFileSync(this.filePath, "utf8"));
    } catch {
      return null;
    }
    if (envelope === null || typeof envelope !== "object") return null;
    const record = envelope as Record<string, unknown>;
    if (record.format !== FORMAT || record.version !== VERSION) return null;
    if (typeof record.ciphertext !== "string") return null;
    try {
      const plaintext = this.safeStorage.decryptString(
        Buffer.from(record.ciphertext, "base64"),
      );
      const tokens = parseSessionTokens(JSON.parse(plaintext));
      if (tokens !== null) this.cached = tokens;
      return tokens;
    } catch {
      return null;
    }
  }

  /**
   * Persist the pair when encryption is available. Returns whether the values
   * reached disk; false means this session lives only in memory.
   */
  save(tokens: SessionTokens): boolean {
    this.cached = tokens;
    if (!this.encryptionAvailable()) return false;
    let ciphertext: Buffer;
    try {
      ciphertext = this.safeStorage.encryptString(JSON.stringify(tokens));
    } catch {
      return false;
    }
    const envelope = JSON.stringify({
      format: FORMAT,
      version: VERSION,
      ciphertext: ciphertext.toString("base64"),
    });
    const temporary = `${this.filePath}.tmp`;
    try {
      mkdirSync(this.directory, { recursive: true });
      writeFileSync(temporary, envelope, { mode: 0o600 });
      renameSync(temporary, this.filePath);
      return true;
    } catch {
      try {
        rmSync(temporary, { force: true });
      } catch {
        // Nothing to clean up, and no token is in the path.
      }
      return false;
    }
  }

  /** Forgetting is always possible, including when the store is read-only. */
  clear(): void {
    this.cached = null;
    try {
      rmSync(this.filePath, { force: true });
    } catch {
      // A failed unlink leaves an encrypted envelope; the in-memory session is
      // already gone and a restart will refuse the stale refresh token.
    }
  }

  /** True when the encrypted file exists; used by tests, not by the renderer. */
  persistedFileExists(): boolean {
    return existsSync(this.filePath);
  }
}
