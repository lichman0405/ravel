/**
 * Gateway endpoint resolution and validation.
 *
 * The desktop connects to exactly one Gateway, and that endpoint is chosen by
 * the operator (environment or the app's own configuration file), never by the
 * renderer and never by a value that arrived over the network. HTTPS is the
 * only production scheme; plain HTTP is admitted only for a loopback address
 * so a developer can run the Gateway on the same machine.
 *
 * No default cloud URL is invented here. V0 has no provisioned public origin,
 * so an unconfigured desktop opens in a clearly labelled configuration state
 * rather than silently talking to an address nobody authorised.
 */

export const GATEWAY_ENV_VAR = "RAVEL_GATEWAY_URL";
export const GATEWAY_CONFIG_FILE = "gateway.json";

export interface GatewayConfig {
  readonly baseUrl: string | null;
  /** Non-secret explanation when `baseUrl` is null or was refused. */
  readonly problem: string | null;
}

export interface FileReader {
  readFileSync(path: string, encoding: "utf8"): string;
}

export interface FileSystemLike {
  readFileSync: FileReader["readFileSync"];
}

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);

/**
 * Validate an operator-supplied Gateway base URL.
 *
 * Returns the normalised string (no trailing slash, no query, no fragment) or
 * throws a TypeError with a message safe to show. Credentials in the URL are
 * refused rather than stripped: a URL that carries a password is a deployment
 * mistake, and silently dropping it would hide which endpoint is really used.
 */
export function validateGatewayUrl(raw: string): string {
  const trimmed = raw.trim();
  if (trimmed.length === 0) {
    throw new TypeError("the Gateway URL is empty");
  }
  let url: URL;
  try {
    url = new URL(trimmed);
  } catch {
    throw new TypeError("the Gateway URL is not a valid URL");
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new TypeError("the Gateway URL must use https (or http on loopback)");
  }
  if (url.username !== "" || url.password !== "") {
    throw new TypeError("the Gateway URL must not contain credentials");
  }
  if (url.search !== "" || url.hash !== "") {
    throw new TypeError("the Gateway URL must not contain a query or fragment");
  }
  const host = url.hostname.toLowerCase();
  if (url.protocol === "http:" && !LOOPBACK_HOSTS.has(host)) {
    throw new TypeError("plain http is allowed only for a loopback Gateway");
  }
  const pathname = url.pathname.replace(/\/+$/, "");
  return `${url.protocol}//${url.host}${pathname}`;
}

function readConfigFile(
  userDataDir: string,
  fs: FileSystemLike,
): { raw: string | null; problem: string | null } {
  const path = `${userDataDir.replace(/[\\/]+$/, "")}/${GATEWAY_CONFIG_FILE}`;
  try {
    return { raw: fs.readFileSync(path, "utf8"), problem: null };
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code === "ENOENT") return { raw: null, problem: null };
    return { raw: null, problem: "the desktop configuration file could not be read" };
  }
}

/**
 * Resolve the Gateway endpoint from an injected environment, filesystem and
 * user-data directory. Injection keeps the rules testable without Electron.
 */
export function resolveGatewayConfig(options: {
  readonly env: Readonly<Record<string, string | undefined>>;
  readonly userDataDir: string;
  readonly fs: FileSystemLike;
}): GatewayConfig {
  const fromEnv = options.env[GATEWAY_ENV_VAR];
  if (fromEnv !== undefined && fromEnv.trim() !== "") {
    try {
      return { baseUrl: validateGatewayUrl(fromEnv), problem: null };
    } catch (error) {
      return {
        baseUrl: null,
        problem: `${GATEWAY_ENV_VAR}: ${(error as Error).message}`,
      };
    }
  }

  const file = readConfigFile(options.userDataDir, options.fs);
  if (file.problem !== null) return { baseUrl: null, problem: file.problem };
  if (file.raw === null) {
    return {
      baseUrl: null,
      problem:
        `No Gateway is configured. Set ${GATEWAY_ENV_VAR} or write ` +
        `${GATEWAY_CONFIG_FILE} in the desktop data directory.`,
    };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(file.raw);
  } catch {
    return { baseUrl: null, problem: `${GATEWAY_CONFIG_FILE} is not valid JSON` };
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { baseUrl: null, problem: `${GATEWAY_CONFIG_FILE} must be an object` };
  }
  const value = (parsed as Record<string, unknown>).gatewayUrl;
  if (typeof value !== "string") {
    return {
      baseUrl: null,
      problem: `${GATEWAY_CONFIG_FILE} must contain a gatewayUrl string`,
    };
  }
  try {
    return { baseUrl: validateGatewayUrl(value), problem: null };
  } catch (error) {
    return {
      baseUrl: null,
      problem: `${GATEWAY_CONFIG_FILE}: ${(error as Error).message}`,
    };
  }
}
