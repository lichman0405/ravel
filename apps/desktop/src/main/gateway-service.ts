/**
 * The one place the desktop talks to the Gateway.
 *
 * It uses the T13-generated `@ravel/api-client` and nothing else: the sign-in,
 * refresh, logout and project-list calls below are generated methods, so the
 * desktop cannot drift into a second interpretation of the v1 contract.
 *
 * The renderer never sees this object. Tokens live in this process and, when
 * the OS encryption is available, in the encrypted session store; every IPC
 * response is a projection that deliberately omits them. The service offers no
 * DAG or contract mutation at all — there is no method here that could change
 * the scientific DAG even if a renderer asked for one.
 */

import {
  ApiError,
  V1Client,
  type Page_ProjectSummary_,
  type ProjectSummary,
  type TokenPair,
} from "@ravel/api-client";

import type {
  ConnectionStatus,
  DesktopSession,
  DesktopUser,
  LoginInput,
} from "../shared/contract";
import type { SessionStore, SessionTokens } from "./session-store";

export type GatewayErrorCode =
  | "CONFIGURATION_REQUIRED"
  | "AUTH_REQUIRED"
  | "INVALID_CREDENTIALS"
  | "GATEWAY_OFFLINE"
  | "GATEWAY_ERROR";

export class GatewayError extends Error {
  constructor(
    readonly code: GatewayErrorCode,
    message: string,
    readonly remoteCode: string | null = null,
  ) {
    super(message);
    this.name = "GatewayError";
  }
}

function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

function pairToTokens(pair: TokenPair): SessionTokens {
  const fields = [
    pair.access_token,
    pair.refresh_token,
    pair.user_id,
    pair.username,
  ];
  if (fields.some((value) => typeof value !== "string" || value.length === 0)) {
    throw new GatewayError("GATEWAY_ERROR", "The Gateway returned an incomplete session.");
  }
  if (typeof pair.expires_in !== "number" || !Number.isFinite(pair.expires_in)) {
    throw new GatewayError("GATEWAY_ERROR", "The Gateway returned an incomplete session.");
  }
  return {
    accessToken: pair.access_token,
    refreshToken: pair.refresh_token,
    userId: pair.user_id,
    username: pair.username,
    expiresIn: pair.expires_in,
    tokenType: pair.token_type ?? "bearer",
  };
}

function userFromTokens(tokens: SessionTokens): DesktopUser {
  return { userId: tokens.userId, username: tokens.username };
}

/**
 * Keep only the fields the read-only shell displays. The generated type is the
 * authority for the shape; copying it explicitly means a future Gateway field
 * does not silently appear in the renderer.
 */
function projectView(project: ProjectSummary): ProjectSummary {
  return {
    display_id: project.display_id,
    objective: project.objective,
    project_id: project.project_id,
    role: project.role,
    status: project.status,
    title: project.title,
  };
}

export interface GatewayServiceOptions {
  readonly baseUrl: string | null;
  readonly configurationProblem: string | null;
  readonly store: SessionStore;
  readonly now?: () => Date;
  readonly onChange?: (status: ConnectionStatus) => void;
  readonly fetchImpl?: typeof fetch;
  readonly timeoutMs?: number;
}

export class GatewayService {
  private readonly baseUrl: string | null;
  private readonly configurationProblem: string | null;
  private readonly store: SessionStore;
  private readonly now: () => Date;
  private readonly onChange: (status: ConnectionStatus) => void;
  private readonly fetchImpl: typeof fetch | undefined;
  private readonly timeoutMs: number;
  private tokens: SessionTokens | null = null;
  private refreshInFlight: Promise<SessionTokens> | null = null;
  private status: ConnectionStatus;

  constructor(options: GatewayServiceOptions) {
    this.baseUrl = options.baseUrl;
    this.configurationProblem = options.configurationProblem;
    this.store = options.store;
    this.now = options.now ?? (() => new Date());
    this.onChange = options.onChange ?? (() => undefined);
    this.fetchImpl = options.fetchImpl;
    this.timeoutMs = options.timeoutMs ?? 8000;
    if (this.baseUrl === null) {
      this.status = this.makeStatus(
        "CONFIGURATION_REQUIRED",
        this.configurationProblem ?? "No Gateway is configured.",
      );
    } else {
      this.status = this.makeStatus("SIGNED_OUT", null);
    }
  }

  private makeStatus(
    state: ConnectionStatus["state"],
    detail: string | null,
  ): ConnectionStatus {
    return { state, detail, checkedAt: this.now().toISOString() };
  }

  private setStatus(state: ConnectionStatus["state"], detail: string | null): void {
    this.status = this.makeStatus(state, detail);
    this.onChange(this.status);
  }

  private client(token: string | (() => string | Promise<string>)): V1Client {
    if (this.baseUrl === null) {
      throw new GatewayError("CONFIGURATION_REQUIRED", "No Gateway is configured.");
    }
    const options = this.fetchImpl
      ? { baseUrl: this.baseUrl, token, fetch: this.fetchImpl }
      : { baseUrl: this.baseUrl, token };
    return new V1Client(options);
  }

  /** Bounded requests: an unreachable Gateway must become OFFLINE, not a hung window. */
  private signal(): AbortSignal {
    return AbortSignal.timeout(this.timeoutMs);
  }

  private currentAccessToken = (): string => {
    if (this.tokens === null) {
      throw new GatewayError("AUTH_REQUIRED", "Sign in to continue.");
    }
    return this.tokens.accessToken;
  };

  private forget(): void {
    this.tokens = null;
    this.store.clear();
  }

  private offline(error: unknown): GatewayError {
    const detail = isApiError(error)
      ? `The Gateway reported HTTP ${error.status}.`
      : "The Gateway could not be reached.";
    this.setStatus("OFFLINE", detail);
    return new GatewayError("GATEWAY_OFFLINE", detail);
  }

  private refused(error: ApiError): GatewayError {
    this.setStatus("ONLINE", error.code);
    return new GatewayError(
      "GATEWAY_ERROR",
      `The Gateway refused the request (${error.code}).`,
      error.code,
    );
  }

  session(): DesktopSession {
    return {
      authenticated: this.tokens !== null,
      user: this.tokens === null ? null : userFromTokens(this.tokens),
      configurationRequired: this.baseUrl === null,
      gatewayOrigin: this.baseUrl === null ? null : new URL(this.baseUrl).origin,
    };
  }

  connection(): ConnectionStatus {
    return this.status;
  }

  async login(input: LoginInput): Promise<DesktopSession> {
    if (this.baseUrl === null) {
      throw new GatewayError("CONFIGURATION_REQUIRED", "No Gateway is configured.");
    }
    const username = input.username.trim();
    if (username.length === 0 || username.length > 128) {
      throw new GatewayError("INVALID_CREDENTIALS", "Enter a username.");
    }
    if (input.password.length === 0 || input.password.length > 1024) {
      throw new GatewayError("INVALID_CREDENTIALS", "Enter a password.");
    }
    let pair: TokenPair;
    try {
      pair = await this.client("").login_v1_auth_login_post({
        body: { username, password: input.password },
        signal: this.signal(),
      });
    } catch (error) {
      if (isApiError(error) && error.status === 401) {
        this.setStatus("ONLINE", error.code);
        throw new GatewayError("INVALID_CREDENTIALS", "The username or password is not correct.");
      }
      if (isApiError(error)) throw this.refused(error);
      throw this.offline(error);
    }
    const tokens = pairToTokens(pair);
    this.tokens = tokens;
    this.store.save(tokens);
    this.setStatus("ONLINE", null);
    return this.session();
  }

  private async performRefresh(refreshToken: string): Promise<SessionTokens> {
    let pair: TokenPair;
    try {
      pair = await this.client("").refresh_v1_auth_refresh_post({
        body: { refresh_token: refreshToken },
        signal: this.signal(),
      });
    } catch (error) {
      if (isApiError(error) && error.status === 401) {
        this.forget();
        this.setStatus("SIGNED_OUT", null);
        throw new GatewayError("AUTH_REQUIRED", "Your session has expired. Sign in again.");
      }
      if (isApiError(error)) throw this.refused(error);
      throw this.offline(error);
    }
    const tokens = pairToTokens(pair);
    this.tokens = tokens;
    this.store.save(tokens);
    this.setStatus("ONLINE", null);
    return tokens;
  }

  /**
   * Rotate the refresh chain once, even when two renderer actions ask at the
   * same moment. The refresh token is single-use; two concurrent rotations
   * would make the second look like a replay and revoke the family.
   */
  private refresh(): Promise<SessionTokens> {
    if (this.refreshInFlight !== null) return this.refreshInFlight;
    const current = this.tokens;
    if (current === null) {
      return Promise.reject(new GatewayError("AUTH_REQUIRED", "Sign in to continue."));
    }
    const inFlight = this.performRefresh(current.refreshToken).finally(() => {
      this.refreshInFlight = null;
    });
    this.refreshInFlight = inFlight;
    return inFlight;
  }

  /**
   * Load the encrypted session, if any, and prove it still works. A network
   * failure keeps the pair so a reconnect can use it; a refused refresh drops
   * it. The caller learns which of those happened only from the status.
   */
  async restore(): Promise<DesktopSession> {
    if (this.baseUrl === null) {
      this.setStatus("CONFIGURATION_REQUIRED", this.configurationProblem);
      return this.session();
    }
    if (this.tokens !== null) return this.session();
    const stored = this.store.load();
    if (stored === null) {
      this.setStatus("SIGNED_OUT", null);
      return this.session();
    }
    this.tokens = stored;
    try {
      await this.refresh();
    } catch (error) {
      if (error instanceof GatewayError && error.code === "AUTH_REQUIRED") {
        return this.session();
      }
      if (!(error instanceof GatewayError && error.code === "GATEWAY_OFFLINE")) {
        this.setStatus("OFFLINE", "The saved session could not be verified.");
      }
    }
    return this.session();
  }

  async listProjects(cursor?: string | null): Promise<Page_ProjectSummary_> {
    if (this.baseUrl === null) {
      throw new GatewayError("CONFIGURATION_REQUIRED", "No Gateway is configured.");
    }
    if (this.tokens === null) {
      throw new GatewayError("AUTH_REQUIRED", "Sign in to continue.");
    }
    if (cursor !== undefined && cursor !== null && cursor.length > 1024) {
      throw new GatewayError("GATEWAY_ERROR", "The project cursor is too long.");
    }
    const query = cursor ? { cursor } : {};
    const read = async (): Promise<Page_ProjectSummary_> => {
      const page = await this.client(this.currentAccessToken).list_projects_v1_projects_get({
        query,
        signal: this.signal(),
      });
      if (page === null || typeof page !== "object" || !Array.isArray(page.items)) {
        throw new GatewayError("GATEWAY_ERROR", "The Gateway returned an invalid project list.");
      }
      return {
        items: page.items.map(projectView),
        ...(page.has_more !== undefined ? { has_more: page.has_more } : {}),
        ...(page.next_cursor !== undefined ? { next_cursor: page.next_cursor } : {}),
      };
    };

    let page: Page_ProjectSummary_;
    try {
      page = await read();
    } catch (error) {
      if (isApiError(error) && error.status === 401) {
        await this.refresh();
        try {
          page = await read();
        } catch (retryError) {
          if (isApiError(retryError)) throw this.refused(retryError);
          throw this.offline(retryError);
        }
      } else if (isApiError(error)) {
        throw this.refused(error);
      } else if (error instanceof GatewayError) {
        throw error;
      } else {
        throw this.offline(error);
      }
    }
    this.setStatus("ONLINE", null);
    return page;
  }

  async logout(): Promise<DesktopSession> {
    const tokens = this.tokens;
    if (tokens !== null && this.baseUrl !== null) {
      try {
        await this.client("").logout_v1_auth_logout_post({
          body: { refresh_token: tokens.refreshToken },
          signal: this.signal(),
        });
      } catch {
        // The local session is cut regardless. A logout that cannot reach the
        // Gateway is reported by the next connection check; keeping the local
        // tokens would be strictly worse.
      }
    }
    this.forget();
    this.setStatus("SIGNED_OUT", null);
    return this.session();
  }

  /** Re-attempt the saved session after an offline period. Never throws. */
  async reconnect(): Promise<ConnectionStatus> {
    if (this.baseUrl === null) {
      this.setStatus("CONFIGURATION_REQUIRED", this.configurationProblem);
      return this.status;
    }
    try {
      if (this.tokens === null) {
        const stored = this.store.load();
        if (stored === null) {
          this.setStatus("SIGNED_OUT", null);
          return this.status;
        }
        this.tokens = stored;
        await this.refresh();
      }
      await this.listProjects();
    } catch (error) {
      if (error instanceof GatewayError && error.code === "AUTH_REQUIRED") {
        this.setStatus("SIGNED_OUT", null);
      } else if (!(error instanceof GatewayError && error.code === "GATEWAY_OFFLINE")) {
        this.setStatus("OFFLINE", "The Gateway could not be reached.");
      }
    }
    return this.status;
  }
}
