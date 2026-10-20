/**
 * Electron hardening as pure functions.
 *
 * This module imports no Electron runtime, so the rules that decide whether an
 * IPC message or a navigation is allowed can be exercised directly. The main
 * process passes real Electron objects with the structural shape below.
 *
 * The rule is deliberately small: one main window, one trusted renderer URL,
 * one main frame, and no navigation or window creation to anywhere else. An
 * IPC sender that cannot be identified is refused, not merely logged.
 */

export interface RendererSecurityPreferences {
  readonly contextIsolation: true;
  readonly nodeIntegration: false;
  readonly nodeIntegrationInWorker: false;
  readonly nodeIntegrationInSubFrames: false;
  readonly sandbox: true;
  readonly webSecurity: true;
  readonly allowRunningInsecureContent: false;
  readonly webviewTag: false;
  readonly navigateOnDragDrop: false;
  readonly spellcheck: false;
  readonly safeDialogs: true;
  readonly preload: string;
}

export interface WebContentsLike {
  readonly id: number;
  isDestroyed(): boolean;
  readonly mainFrame?: unknown;
}

export interface FrameLike {
  readonly url: string;
  readonly parent: unknown | null;
}

export interface IpcSenderEventLike {
  readonly sender: WebContentsLike;
  readonly senderFrame: FrameLike | null;
}

/**
 * Preferences used for the only BrowserWindow the app creates. `sandbox: true`
 * means the preload runs without Node globals even if a future dependency
 * would prefer them; `contextIsolation` keeps the bridge in its own world.
 */
export function secureWebPreferences(preloadPath: string): RendererSecurityPreferences {
  return Object.freeze({
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
    preload: preloadPath,
  });
}

function comparable(url: URL): string {
  const path =
    url.protocol === "file:" ? url.pathname.toLowerCase() : url.pathname;
  return `${url.protocol}//${url.host}${path}`;
}

/**
 * Whether a URL is the renderer document and nothing else.
 *
 * The base document's hash is ignored because an in-page anchor does not load
 * a new document. Everything else — a different file, a data: URL, a remote
 * origin, a query — is not the application and is refused.
 */
export function isTrustedRendererUrl(candidate: string, trusted: string): boolean {
  let left: URL;
  let right: URL;
  try {
    left = new URL(candidate);
    right = new URL(trusted);
  } catch {
    return false;
  }
  if (left.search !== "" || right.search !== "") return false;
  if (left.hash !== "") left.hash = "";
  if (right.hash !== "") right.hash = "";
  return comparable(left) === comparable(right);
}

/** Navigation is allowed only to the exact renderer document. */
export function navigationAllowed(target: string, trustedRendererUrl: string): boolean {
  return isTrustedRendererUrl(target, trustedRendererUrl);
}

/**
 * Whether an IPC message came from the main frame of the main window.
 *
 * Electron's `event.senderFrame` can be null after a frame is destroyed; that
 * case is a refusal. `sender.mainFrame` identity is checked when Electron
 * exposes it, so a same-URL subframe is still not accepted.
 */
export function isTrustedIpcSender(
  event: IpcSenderEventLike,
  mainContents: WebContentsLike | null,
  trustedRendererUrl: string,
): boolean {
  if (mainContents === null) return false;
  if (event.sender !== mainContents) return false;
  if (event.sender.isDestroyed()) return false;
  const frame = event.senderFrame;
  if (frame === null) return false;
  if (frame.parent !== null) return false;
  if (!isTrustedRendererUrl(frame.url, trustedRendererUrl)) return false;
  if (mainContents.mainFrame !== undefined && frame !== mainContents.mainFrame) {
    return false;
  }
  return true;
}

/** No renderer may open a second window, whatever the URL says. */
export function windowOpenDecision(): { readonly action: "deny" } {
  return { action: "deny" };
}

/** No web permission (camera, microphone, notifications, geolocation) is used. */
export function permissionRequestAllowed(): boolean {
  return false;
}
