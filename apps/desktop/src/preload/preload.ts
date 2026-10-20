/**
 * The sandboxed preload bridge.
 *
 * This is the entire renderer-facing surface. It has no imports beyond
 * Electron's context bridge and the frozen channel constants, so there is no
 * `require`, no filesystem, no environment and no dynamic channel name. The
 * main process still validates the sender and registers only these handlers.
 */

import { contextBridge, ipcRenderer } from "electron";

import {
  BRIDGE_GLOBAL,
  BRIDGE_VERSION,
  IPC_CHANNELS,
  type BridgeApi,
  type ConnectionStatus,
  type DesktopSession,
  type LoginInput,
  type ProjectPage,
} from "../shared/contract";

const api: BridgeApi = Object.freeze({
  version: BRIDGE_VERSION,
  login: (input: LoginInput): Promise<DesktopSession> =>
    ipcRenderer.invoke(IPC_CHANNELS.login, input),
  logout: (): Promise<DesktopSession> => ipcRenderer.invoke(IPC_CHANNELS.logout),
  session: (): Promise<DesktopSession> => ipcRenderer.invoke(IPC_CHANNELS.session),
  listProjects: (cursor?: string | null): Promise<ProjectPage> =>
    ipcRenderer.invoke(
      IPC_CHANNELS.listProjects,
      cursor === undefined || cursor === null ? {} : { cursor },
    ),
  connection: (): Promise<ConnectionStatus> =>
    ipcRenderer.invoke(IPC_CHANNELS.connection),
  reconnect: (): Promise<ConnectionStatus> =>
    ipcRenderer.invoke(IPC_CHANNELS.reconnect),
  onConnectionStatus: (listener: (status: ConnectionStatus) => void): (() => void) => {
    const wrapped = (_event: unknown, status: ConnectionStatus): void => {
      listener(status);
    };
    ipcRenderer.on(IPC_CHANNELS.connectionStatus, wrapped);
    return () => {
      ipcRenderer.removeListener(IPC_CHANNELS.connectionStatus, wrapped);
    };
  },
});

contextBridge.exposeInMainWorld(BRIDGE_GLOBAL, api);
