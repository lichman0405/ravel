/**
 * The Electron main process for the RAVEL desktop shell.
 *
 * It owns the window, the session and the Gateway connection. The renderer is
 * sandboxed, isolated and has no Node globals; it can only call the named
 * methods the preload exposes, and every one of those checks that the message
 * came from the main frame of this window at this app's own URL.
 *
 * There is deliberately no shell execution, no filesystem bridge, no external
 * URL opener and no generic IPC channel. A renderer that is completely hostile
 * still cannot reach a token, a credential, the environment or the machine.
 */

import {
  app,
  BrowserWindow,
  ipcMain,
  Menu,
  safeStorage,
  session,
  type IpcMainInvokeEvent,
  type WebContents,
} from "electron";
import { readFileSync } from "node:fs";
import { isAbsolute, join } from "node:path";
import { pathToFileURL } from "node:url";

import {
  IPC_CHANNELS,
  type ConnectionStatus,
  type InvokeChannel,
  type LoginInput,
} from "../shared/contract";
import { resolveGatewayConfig } from "./config";
import { GatewayError, GatewayService } from "./gateway-service";
import {
  isTrustedIpcSender,
  navigationAllowed,
  permissionRequestAllowed,
  secureWebPreferences,
  windowOpenDecision,
} from "./security";
import { SessionStore } from "./session-store";

const TRUSTED_DEV_RENDERER = process.env.RAVEL_DESKTOP_RENDERER_URL;
const USER_DATA_OVERRIDE = process.env.RAVEL_DESKTOP_USER_DATA;

let mainWindow: BrowserWindow | null = null;
let trustedRendererUrl = "";
let lastStatus: ConnectionStatus | null = null;

function applyUserDataOverride(): void {
  if (!USER_DATA_OVERRIDE) return;
  if (!isAbsolute(USER_DATA_OVERRIDE)) {
    throw new Error("RAVEL_DESKTOP_USER_DATA must be an absolute path");
  }
  app.setPath("userData", USER_DATA_OVERRIDE);
}

function rendererEntryUrl(): string {
  if (TRUSTED_DEV_RENDERER) {
    // A development renderer is an explicit operator choice. It is still only
    // one exact URL; anything else is refused by the same check.
    return new URL(TRUSTED_DEV_RENDERER).href;
  }
  return pathToFileURL(join(__dirname, "..", "renderer", "index.html")).href;
}

function readString(
  payload: unknown,
  key: string,
  maxLength: number,
): string | null {
  if (payload === null || typeof payload !== "object") return null;
  const value = (payload as Record<string, unknown>)[key];
  if (typeof value !== "string" || value.length > maxLength) return null;
  return value;
}

function readLogin(payload: unknown): LoginInput {
  const username = readString(payload, "username", 128);
  const password = readString(payload, "password", 1024);
  if (username === null || password === null) {
    throw new Error("Enter a username and a password.");
  }
  return { username, password };
}

function readCursor(payload: unknown): string | null {
  if (payload === null || typeof payload !== "object") return null;
  const cursor = (payload as Record<string, unknown>).cursor;
  if (cursor === undefined || cursor === null) return null;
  if (typeof cursor !== "string" || cursor.length > 1024) {
    throw new Error("The project cursor is invalid.");
  }
  return cursor;
}

/**
 * Convert an internal failure into a renderer-safe Error. The renderer gets a
 * sentence, not a stack, a URL, a request body or a token. Unknown failures
 * are logged by name only.
 */
function publicError(error: unknown): Error {
  if (error instanceof GatewayError) return new Error(error.message);
  if (error instanceof Error && error.message.length <= 200) {
    // Validation messages from this file are already static sentences.
    return new Error(error.message);
  }
  console.error("[desktop] request failed");
  return new Error("The desktop client could not complete the request.");
}

function publishStatus(status: ConnectionStatus): void {
  lastStatus = status;
  if (mainWindow === null || mainWindow.isDestroyed()) return;
  mainWindow.webContents.send(IPC_CHANNELS.connectionStatus, status);
}

function assertTrustedSender(event: IpcMainInvokeEvent): void {
  if (
    !isTrustedIpcSender(
      { sender: event.sender, senderFrame: event.senderFrame },
      mainWindow?.webContents ?? null,
      trustedRendererUrl,
    )
  ) {
    throw new Error("IPC sender refused");
  }
}

function registerHandler<T>(
  channel: InvokeChannel,
  handler: (payload: unknown) => Promise<T> | T,
): void {
  ipcMain.handle(channel, async (event: IpcMainInvokeEvent, payload: unknown) => {
    assertTrustedSender(event);
    try {
      return await handler(payload);
    } catch (error) {
      throw publicError(error);
    }
  });
}

function hardenWebContents(contents: WebContents): void {
  contents.setWindowOpenHandler(() => windowOpenDecision());
  const denyForeignNavigation = (
    event: { preventDefault(): void },
    url: string,
  ): void => {
    const isMain = mainWindow !== null && contents === mainWindow.webContents;
    if (!isMain || !navigationAllowed(url, trustedRendererUrl)) {
      event.preventDefault();
    }
  };
  contents.on("will-navigate", (event, url) => denyForeignNavigation(event, url));
  contents.on("will-redirect", (event, url) => denyForeignNavigation(event, url));
  contents.on("will-frame-navigate", (event) => {
    if (!navigationAllowed(event.url, trustedRendererUrl)) event.preventDefault();
  });
  contents.on("will-attach-webview", (event) => event.preventDefault());
}

function configureSession(): void {
  const defaultSession = session.defaultSession;
  defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => {
    callback(permissionRequestAllowed());
  });
  defaultSession.setPermissionCheckHandler(() => permissionRequestAllowed());
}

function createWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1100,
    height: 760,
    minWidth: 760,
    minHeight: 540,
    show: false,
    backgroundColor: "#0b1424",
    title: "RAVEL",
    webPreferences: {
      ...secureWebPreferences(join(__dirname, "preload.cjs")),
    },
  });
  window.once("ready-to-show", () => window.show());
  window.on("closed", () => {
    if (mainWindow === window) mainWindow = null;
  });
  window.webContents.on("did-finish-load", () => {
    if (lastStatus !== null) {
      window.webContents.send(IPC_CHANNELS.connectionStatus, lastStatus);
    }
  });
  window.webContents.on("render-process-gone", (_event, details) => {
    console.error(`[desktop] renderer gone: ${details.reason}`);
  });
  window
    .loadFile(join(__dirname, "..", "renderer", "index.html"))
    .catch(() => console.error("[desktop] renderer failed to load"));
  return window;
}

async function start(): Promise<void> {
  applyUserDataOverride();
  app.setName("RAVEL Desktop");
  Menu.setApplicationMenu(null);

  const userDataDir = app.getPath("userData");
  const config = resolveGatewayConfig({
    env: process.env,
    userDataDir,
    fs: { readFileSync: (path) => readFileSync(path, "utf8") },
  });
  const store = new SessionStore(userDataDir, safeStorage);
  const gateway = new GatewayService({
    baseUrl: config.baseUrl,
    configurationProblem: config.problem,
    store,
    onChange: publishStatus,
  });

  configureSession();
  app.on("web-contents-created", (_event, contents) => {
    hardenWebContents(contents);
  });

  registerHandler(IPC_CHANNELS.login, (payload) => gateway.login(readLogin(payload)));
  registerHandler(IPC_CHANNELS.logout, () => gateway.logout());
  registerHandler(IPC_CHANNELS.session, () => gateway.session());
  registerHandler(IPC_CHANNELS.listProjects, (payload) =>
    gateway.listProjects(readCursor(payload)),
  );
  registerHandler(IPC_CHANNELS.connection, () => gateway.connection());
  registerHandler(IPC_CHANNELS.reconnect, () => gateway.reconnect());

  trustedRendererUrl = rendererEntryUrl();
  await gateway.restore();
  lastStatus = gateway.connection();
  mainWindow = createWindow();
}

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  // macOS dock activation, where the process is still the same one and the
  // registered IPC handlers still close over the same Gateway service.
  if (mainWindow === null && app.isReady()) mainWindow = createWindow();
});

app
  .whenReady()
  .then(start)
  .catch((error: unknown) => {
    console.error(
      `[desktop] failed to start: ${error instanceof Error ? error.name : "unknown"}`,
    );
    app.exit(1);
  });
