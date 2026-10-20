import type { BridgeApi } from "../../shared/contract";

declare global {
  interface Window {
    readonly ravelDesktop: BridgeApi;
  }
}

export {};
