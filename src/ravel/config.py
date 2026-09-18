"""Process-wide configuration.

Every setting is read from the environment, optionally seeded by the repository
`.env` file. Nothing here reads a secret from a checked-in file: `.env` is
git-ignored and `.env.example` documents the names.

`DEEPSEEK_API_KEY` deliberately carries no `RAVEL_` prefix. It is the name the
pinned harness resolves per request through its `apiKeyEnv` config key, and the
inherited environment outranks every file layer the harness consults, so a value
set here is authoritative for RAVEL-launched runtimes.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """RAVEL runtime configuration, resolved once per process."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="RAVEL_",
        extra="ignore",
        case_sensitive=False,
    )

    env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # Runtime state owned by RAVEL: DSH homes, workspaces, research snapshots.
    # Never placed inside a workspace an agent can influence.
    runtime_dir: Path = Path("./runtime")

    # ── Authoritative state ────────────────────────────────────────────────
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 55432
    postgres_db: str = "ravel"
    postgres_user: str = "ravel"
    postgres_password: SecretStr = SecretStr("ravel_dev_password")
    postgres_dsn_override: str | None = Field(default=None, alias="RAVEL_POSTGRES_DSN")

    # ── Durable execution ──────────────────────────────────────────────────
    temporal_host: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "ravel-v0"

    # ── Object storage ─────────────────────────────────────────────────────
    # Artifact bytes live here. They never enter PostgreSQL.
    s3_endpoint: str = "http://127.0.0.1:9100"
    s3_access_key: str = "ravel_minio"
    s3_secret_key: SecretStr = SecretStr("ravel_minio_dev_password")
    s3_bucket: str = "ravel-artifacts"
    s3_region: str = "us-east-1"

    # ── Harness ────────────────────────────────────────────────────────────
    dsh_home: Path = Path("./runtime/dsh_home")
    dsh_bin: str | None = None
    # The base profile is fixed rather than configurable: the role overlay
    # targets row ids that only `sdk-minimal` defines, so changing it would
    # silently drop a role's prompt or leave the shell in place.
    dsh_provider: str = "deepseek-official"
    # The SDK default (`deepseek-v4-flash`) is not served by every account, so
    # RAVEL names the model explicitly rather than inheriting a guess.
    dsh_model: str = "deepseek-flash"
    dsh_reasoning_effort: str | None = None
    # A runtime process is reaped after this long without a turn. Bounds the
    # number of live Node processes on a single CVM.
    dsh_idle_timeout_seconds: int = 900
    dsh_turn_timeout_seconds: float | None = 1800.0

    # ── Model credentials ──────────────────────────────────────────────────
    deepseek_api_key: SecretStr | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str | None = Field(default=None, alias="DEEPSEEK_BASE_URL")

    # ── Research ───────────────────────────────────────────────────────────
    research_contact_email: str | None = None
    search_api_key: SecretStr | None = None
    search_provider: str | None = None
    playwright_headless: bool = True

    # ── Gateway ────────────────────────────────────────────────────────────
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 8000
    gateway_jwt_secret: SecretStr = SecretStr("dev-only-change-me")
    gateway_token_ttl_seconds: int = 3600

    @field_validator(
        "dsh_bin",
        "dsh_reasoning_effort",
        "deepseek_base_url",
        "research_contact_email",
        "search_provider",
        "postgres_dsn_override",
        mode="before",
    )
    @classmethod
    def _blank_is_absent(cls, value: object) -> object:
        """Treat a blank environment value as unset.

        `.env.example` ships optional keys as `KEY=`, so an empty string is the
        normal way to say "not configured". Left as-is, `dsh_bin=""` would
        resolve to the current directory and be executed as the harness binary.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("deepseek_api_key", "search_api_key", mode="before")
    @classmethod
    def _blank_secret_is_absent(cls, value: object) -> object:
        """Treat a blank secret as unset, so an empty `.env` key is not a credential."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # ── Derived paths ──────────────────────────────────────────────────────

    @computed_field  # type: ignore[prop-decorator]
    @property
    def repo_root(self) -> Path:
        """The repository checkout this process was started from."""
        return REPO_ROOT

    @property
    def postgres_dsn(self) -> str:
        """SQLAlchemy DSN for the authoritative Project State database."""
        if self.postgres_dsn_override:
            return self.postgres_dsn_override
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql+psycopg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @staticmethod
    def _path_component(part: str) -> str:
        """Reject a path fragment that could escape the runtime root.

        Project ids reach this method from the database, the API, and the TUI,
        so they are untrusted input rather than constants. A component that is
        empty, a directory reference, or contains a separator is refused before
        anything is joined or created.

        Raises:
            ValueError: The fragment is not usable as a single path component.
        """
        if not part or part in {".", ".."} or "/" in part or "\\" in part or "\x00" in part:
            raise ValueError(f"{part!r} is not usable as a path component")
        return part

    def runtime_path(self, *parts: str) -> Path:
        """A path under the runtime root, created on demand.

        Raises:
            ValueError: A component would escape the runtime root.
        """
        root = (REPO_ROOT / self.runtime_dir).resolve()
        path = root.joinpath(*(self._path_component(part) for part in parts)).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"{path} escapes the runtime root {root}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def dsh_home_path(self) -> Path:
        """The DSH home shared by every RAVEL-launched runtime process.

        Profiles and session logs accumulate under this root. It sits outside
        every research workspace on purpose: the pinned harness reads
        `<cwd>/.env` as a credential fallback, so a runtime must never be
        launched with a working directory an agent or a fetched page can write
        to.
        """
        path = (REPO_ROOT / self.dsh_home).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def dsh_runtime_cwd(self, project_id: str, role: str) -> Path:
        """An empty, RAVEL-owned working directory for one runtime process.

        Agents reach project data through RAVEL MCP tools that carry their
        authority in the process environment, so this directory is never a
        source of truth and never needs to hold anything.
        """
        return self.runtime_path("dsh_cwd", project_id, role)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process settings, constructed once."""
    return Settings()
