/**
 * The contract between the Electron main process and the single renderer.
 *
 * There is no generic `invoke` on this bridge. Every capability is a named
 * method, the main process registers exactly these channels, and the preload
 * script is the only code that may call `ipcRenderer`. A renderer that is
 * compromised can therefore ask for a project list or a sign-in, and nothing
 * else; it cannot name an arbitrary channel and it never receives a token.
 *
 * Types in this module are deliberately structural. The generated T13 client
 * remains the owner of the Gateway projections; this file re-exports its types
 * to the renderer instead of restating them.
 */

import type { Page_ProjectSummary_, ProjectSummary } from "@ravel/api-client";

/** Bumped only when the preload surface changes incompatibly. */
export const BRIDGE_VERSION = 1;
/** Tests and the preload use this marker; a renderer must never assume more. */
export const BRIDGE_GLOBAL = "ravelDesktop";

export const IPC_CHANNELS = Object.freeze({
  login: "ravel:auth:login",
  logout: "ravel:auth:logout",
  session: "ravel:session:read",
  listProjects: "ravel:projects:list",
  connection: "ravel:connection:read",
  reconnect: "ravel:connection:reconnect",
  connectionStatus: "ravel:connection:status",
} as const);

export type InvokeChannel =
  | typeof IPC_CHANNELS.login
  | typeof IPC_CHANNELS.logout
  | typeof IPC_CHANNELS.session
  | typeof IPC_CHANNELS.listProjects
  | typeof IPC_CHANNELS.connection
  | typeof IPC_CHANNELS.reconnect;

/** The exact invoke channels the main process registers and the preload may use. */
export const INVOKE_CHANNELS = Object.freeze([
  IPC_CHANNELS.login,
  IPC_CHANNELS.logout,
  IPC_CHANNELS.session,
  IPC_CHANNELS.listProjects,
  IPC_CHANNELS.connection,
  IPC_CHANNELS.reconnect,
] as const satisfies readonly InvokeChannel[]);

/** Main-to-renderer notifications. The renderer cannot send on these. */
export const EVENT_CHANNELS = Object.freeze([IPC_CHANNELS.connectionStatus] as const);

export function isInvokeChannel(value: string): value is InvokeChannel {
  return (INVOKE_CHANNELS as readonly string[]).includes(value);
}

export interface LoginInput {
  readonly username: string;
  readonly password: string;
}

export interface DesktopUser {
  readonly userId: string;
  readonly username: string;
}

export interface DesktopSession {
  readonly authenticated: boolean;
  readonly user: DesktopUser | null;
  readonly configurationRequired: boolean;
  readonly gatewayOrigin: string | null;
}

export type ConnectionState =
  | "SIGNED_OUT"
  | "ONLINE"
  | "OFFLINE"
  | "CONFIGURATION_REQUIRED";

export interface ConnectionStatus {
  readonly state: ConnectionState;
  /** Safe for display: a Gateway error code or a configuration sentence. */
  readonly detail: string | null;
  readonly checkedAt: string;
}

/** What the renderer may see of a project. The generated client owns this shape. */
export type ProjectPage = Page_ProjectSummary_;
export type ProjectView = ProjectSummary;

export interface BridgeApi {
  readonly version: number;
  login(input: LoginInput): Promise<DesktopSession>;
  logout(): Promise<DesktopSession>;
  session(): Promise<DesktopSession>;
  listProjects(cursor?: string | null): Promise<ProjectPage>;
  connection(): Promise<ConnectionStatus>;
  reconnect(): Promise<ConnectionStatus>;
  /** Subscribe to connection changes; returns an unsubscribe function. */
  onConnectionStatus(listener: (status: ConnectionStatus) => void): () => void;
}
