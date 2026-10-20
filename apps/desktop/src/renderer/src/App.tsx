import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactElement,
} from "react";

import type {
  ConnectionStatus,
  DesktopSession,
  ProjectView,
} from "../../shared/contract";

const bridge = window.ravelDesktop;

function messageFrom(error: unknown): string {
  if (!(error instanceof Error)) return "The request could not be completed.";
  // Electron prefixes rejected IPC invocations with the channel name. The
  // sentence after the last "Error: " is the only part meant for a person.
  const marker = "Error: ";
  const index = error.message.lastIndexOf(marker);
  const message = index >= 0 ? error.message.slice(index + marker.length) : error.message;
  return message.length > 0 && message.length <= 300
    ? message
    : "The request could not be completed.";
}

function statusLabel(status: ConnectionStatus | null): string {
  switch (status?.state) {
    case "ONLINE":
      return "Connected";
    case "OFFLINE":
      return "Offline";
    case "CONFIGURATION_REQUIRED":
      return "Gateway not configured";
    case "SIGNED_OUT":
      return "Signed out";
    default:
      return "Connecting";
  }
}

export function App(): ReactElement {
  const [session, setSession] = useState<DesktopSession | null>(null);
  const [status, setStatus] = useState<ConnectionStatus | null>(null);
  const [projects, setProjects] = useState<ProjectView[]>([]);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [projectsLoading, setProjectsLoading] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loginError, setLoginError] = useState<string | null>(null);
  const [signingIn, setSigningIn] = useState(false);
  const mounted = useRef(true);

  const loadProjects = useCallback(async (): Promise<void> => {
    setProjectsLoading(true);
    setProjectsError(null);
    try {
      const page = await bridge.listProjects();
      if (!mounted.current) return;
      setProjects(page.items);
    } catch (error) {
      if (!mounted.current) return;
      setProjectsError(messageFrom(error));
    } finally {
      if (mounted.current) setProjectsLoading(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    const unsubscribe = bridge.onConnectionStatus((next) => {
      if (mounted.current) setStatus(next);
    });
    void (async () => {
      try {
        const current = await bridge.session();
        const connection = await bridge.connection();
        if (!mounted.current) return;
        setSession(current);
        setStatus(connection);
        if (current.authenticated) await loadProjects();
      } catch (error) {
        if (mounted.current) setLoginError(messageFrom(error));
      }
    })();
    return () => {
      mounted.current = false;
      unsubscribe();
    };
  }, [loadProjects]);

  async function submitLogin(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (signingIn) return;
    setSigningIn(true);
    setLoginError(null);
    try {
      const next = await bridge.login({ username, password });
      if (!mounted.current) return;
      setSession(next);
      setPassword("");
      await loadProjects();
    } catch (error) {
      if (mounted.current) setLoginError(messageFrom(error));
    } finally {
      if (mounted.current) setSigningIn(false);
    }
  }

  async function signOut(): Promise<void> {
    try {
      const next = await bridge.logout();
      if (!mounted.current) return;
      setSession(next);
      setProjects([]);
      setProjectsError(null);
      setPassword("");
    } catch (error) {
      if (mounted.current) setLoginError(messageFrom(error));
    }
  }

  async function reconnect(): Promise<void> {
    try {
      const connection = await bridge.reconnect();
      if (!mounted.current) return;
      setStatus(connection);
      const current = await bridge.session();
      if (!mounted.current) return;
      setSession(current);
      if (current.authenticated) await loadProjects();
    } catch (error) {
      if (mounted.current) setProjectsError(messageFrom(error));
    }
  }

  if (session === null) {
    return (
      <main className="center">
        <section className="card">
          <p className="eyebrow">RAVEL</p>
          <h1>Opening the research console...</h1>
          <p className="muted">Checking the Gateway and the local session.</p>
          {loginError ? <p className="error">{loginError}</p> : null}
        </section>
      </main>
    );
  }

  if (session.configurationRequired) {
    return (
      <main className="center">
        <section className="card">
          <p className="eyebrow">RAVEL</p>
          <h1>Gateway not configured</h1>
          <p className="muted">
            Set RAVEL_GATEWAY_URL (or gateway.json in the desktop data directory)
            to an operator-approved Gateway. No endpoint is guessed by the app.
          </p>
          {status?.detail ? <p className="detail">{status.detail}</p> : null}
          <button type="button" onClick={() => void reconnect()}>
            Check configuration
          </button>
          {loginError ? <p className="error">{loginError}</p> : null}
        </section>
      </main>
    );
  }

  if (!session.authenticated) {
    return (
      <main className="center">
        <section className="card">
          <p className="eyebrow">RAVEL</p>
          <h1>Autonomous research, under review.</h1>
          <p className="muted">
            Sign in with your RAVEL account. The desktop is a thin client: the
            Gateway owns project state and remains running when this window closes.
          </p>
          <p className={`status status-${status?.state.toLowerCase() ?? "pending"}`}>
            {statusLabel(status)}
          </p>
          {status?.detail ? <p className="detail">{status.detail}</p> : null}
          <form onSubmit={(event) => void submitLogin(event)}>
            <label htmlFor="username">Username</label>
            <input
              id="username"
              name="username"
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              maxLength={128}
              required
              disabled={signingIn}
            />
            <label htmlFor="password">Password</label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              maxLength={1024}
              required
              disabled={signingIn}
            />
            <button type="submit" disabled={signingIn}>
              {signingIn ? "Signing in..." : "Sign in"}
            </button>
          </form>
          {loginError ? <p className="error">{loginError}</p> : null}
          <p className="footnote">
            Credentials go directly to the configured Gateway. If the operating
            system encryption is available, the desktop keeps only an encrypted
            session file; otherwise the session is kept in memory.
          </p>
        </section>
      </main>
    );
  }

  return (
    <div className="app">
      <header className="topbar">
        <div>
          <p className="eyebrow">RAVEL</p>
          <h1>Research console</h1>
        </div>
        <div className="identity">
          <span className={`status status-${status?.state.toLowerCase() ?? "pending"}`}>
            {statusLabel(status)}
          </span>
          <span className="user">{session.user?.username}</span>
          <button type="button" className="secondary" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </header>

      {status?.state === "OFFLINE" ? (
        <div className="banner" role="status">
          <strong>The Gateway is offline.</strong>{" "}
          {status.detail ?? "Project data cannot be refreshed."}
          <button type="button" className="secondary" onClick={() => void reconnect()}>
            Reconnect
          </button>
        </div>
      ) : null}

      <main className="content">
        <section className="panel" aria-labelledby="projects-heading">
          <div className="panel-heading">
            <div>
              <h2 id="projects-heading">Projects</h2>
              <p className="muted small">
                Read-only. The scientific DAG and every decision belong to the
                Gateway and the Master agent.
              </p>
            </div>
            <button
              type="button"
              className="secondary"
              onClick={() => void loadProjects()}
              disabled={projectsLoading}
            >
              {projectsLoading ? "Refreshing..." : "Refresh"}
            </button>
          </div>

          {projectsError ? <p className="error">{projectsError}</p> : null}
          {projectsLoading && projects.length === 0 ? (
            <p className="muted">Loading projects...</p>
          ) : null}
          {!projectsLoading && projects.length === 0 && projectsError === null ? (
            <p className="muted">No projects are visible to this account.</p>
          ) : null}

          <ul className="projects">
            {projects.map((project) => (
              <li key={project.project_id} className="project">
                <div className="project-title">
                  <span className="code">{project.display_id}</span>
                  <h3>{project.title}</h3>
                </div>
                <p className="objective">{project.objective}</p>
                <dl className="meta">
                  <div>
                    <dt>Status</dt>
                    <dd>{project.status}</dd>
                  </div>
                  <div>
                    <dt>Your role</dt>
                    <dd>{project.role}</dd>
                  </div>
                </dl>
              </li>
            ))}
          </ul>
          <p className="footnote">
            Project details, the DAG, evidence and computations arrive in the
            next desktop milestone. This shell deliberately offers no mutation.
          </p>
        </section>
      </main>
    </div>
  );
}
