"""What a Gateway has to be configured with before it will answer anyone.

The development defaults exist so that `make up` works on a laptop, and every
one of them is public: the JWT key is in this repository, the database password
is in `docker-compose.yml`, the object-store secret is in the same file. That
is the right default for a process reachable only from the machine that
started it and the wrong one for a Gateway on a CVM, so the composition root
asks this module whether the settings it was handed are *deployment* settings
before it opens a port. The check runs whether or not a token service was
injected: a test may supply its own tokens, but production startup must not be
bypassable by doing so.

Three groups of rules:

**Credentials.** The shipped values are refused, and the message names the
variable without printing the value. A DSN override is also examined, because
the effective password is the one inside it and a deployment that replaced the
field while leaving the DSN at its development value would otherwise pass.

**Where it listens.** A production bind has to be loopback, private, or
link-local — that is the "reverse proxy on the same host" shape V0 deploys —
unless the operator sets `RAVEL_GATEWAY_ALLOW_PUBLIC_BIND`. An unspecified
address such as `0.0.0.0` is not private, however Python classifies it: it is
every interface, which is the opposite of the default this rule wants.

**What a browser is allowed to be.** A production Gateway is reached over
HTTPS at one public origin, and the WebSocket route compares an `Origin`
header to an exact set. The public origin has to be one of those entries, and
no entry may contain a path, query, fragment, or userinfo. A wildcard is
refused rather than accepted-and-ignored: the socket carries a bearer token in
its handshake, and a cross-origin page that can open one is exactly what the
allowlist exists to stop.

The same module also validates the two bounds that are not about credentials:
the stream reauthorization interval, which the task caps at five seconds, and
the upload limit, which has to be a positive number of bytes. They are checked
in every environment because a negative limit is a bug wherever it is set.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from ravel.config import Settings
from ravel.gateway.auth.tokens import require_a_real_secret

#: The database password shipped in `config.Settings`.
DEV_POSTGRES_PASSWORD = "ravel_dev_password"

#: The object-store secret shipped in `config.Settings`.
DEV_S3_SECRET = "ravel_minio_dev_password"

#: The documented ceiling for how long a revoked member may keep receiving
#: events. The route is built to this number and the settings validator refuses
#: a larger one; lowering it is a deployment choice.
MAX_STREAM_REAUTHORIZE_SECONDS = 5.0


def parse_origins(entries: str) -> tuple[str, ...]:
    """The configured origins, normalized and checked, in configuration order.

    Normalization is what makes an exact comparison possible. A browser
    serializes an origin as `scheme://host[:port]`, lowercased, with a default
    port omitted; a person configuring one writes `https://example.org/` as
    often as not. Both spellings describe the same origin and both normalize to
    the same string here.

    An unconfigured value is no origins rather than a malformed one: the same
    rule `exact_origins` documents, held here so the two cannot disagree about
    what an empty setting means. An empty entry inside a configured list is
    still a typo, and still refused.

    Raises:
        ValueError: An entry is not a bare `http`/`https` origin.
    """
    text = entries.strip()
    if not text:
        return ()
    return tuple(_origin(entry, "RAVEL_GATEWAY_ALLOWED_ORIGINS") for entry in text.split(","))


def exact_origins(settings: Settings) -> frozenset[str]:
    """The WebSocket `Origin` allowlist as a set, normalized.

    An empty configuration is an empty set rather than an error: a terminal
    client sends no `Origin` and is unaffected, and every request that does
    present one is refused until a deployment names the origins it serves.
    """
    if not settings.gateway_allowed_origins.strip():
        return frozenset()
    return frozenset(parse_origins(settings.gateway_allowed_origins))


def validate_gateway_settings(settings: Settings) -> None:
    """Refuse to start a Gateway that would be reachable and undefended.

    Raises:
        ValueError: A production credential is the shipped default, the bind
            address is public without permission, a browser-facing origin is
            missing or malformed, or one of the two numeric bounds is outside
            its range.
    """
    if settings.gateway_upload_limit_bytes <= 0:
        raise ValueError("RAVEL_GATEWAY_UPLOAD_LIMIT_BYTES must be a positive number of bytes")
    if not 0 < settings.gateway_stream_reauthorize_seconds <= MAX_STREAM_REAUTHORIZE_SECONDS:
        raise ValueError(
            "RAVEL_GATEWAY_STREAM_REAUTHORIZE_SECONDS must be greater than zero "
            f"and at most {MAX_STREAM_REAUTHORIZE_SECONDS:g}; a slower recheck "
            "would let a revoked membership keep receiving events for longer "
            "than the documented bound"
        )

    # A public origin and an allowlist are read in every environment when they
    # are set, so that a malformed value is reported by the process that would
    # have used it rather than by the browser that silently refuses it.
    public_origin = (
        _origin(settings.gateway_public_origin, "RAVEL_GATEWAY_PUBLIC_ORIGIN")
        if settings.gateway_public_origin
        else None
    )
    allowed = exact_origins(settings)

    if public_origin is not None:
        if urlsplit(public_origin).scheme != "https" and settings.env == "production":
            raise ValueError(
                "RAVEL_GATEWAY_PUBLIC_ORIGIN has to be an https origin in production; "
                "the reverse proxy is the only thing that terminates TLS"
            )
        if settings.env == "production" and public_origin not in allowed:
            raise ValueError(
                "RAVEL_GATEWAY_ALLOWED_ORIGINS has to contain the value of "
                "RAVEL_GATEWAY_PUBLIC_ORIGIN, or the browser the deployment "
                "serves cannot open the event stream"
            )

    if settings.env != "production":
        return

    require_a_real_secret(
        settings.gateway_jwt_secret.get_secret_value(), environment=settings.env
    )

    if settings.postgres_password.get_secret_value() == DEV_POSTGRES_PASSWORD:
        raise ValueError(
            "RAVEL_POSTGRES_PASSWORD is the shipped development default; in "
            "production it is public, so set the database credential the "
            "deployment actually uses"
        )
    if settings.postgres_dsn_override and DEV_POSTGRES_PASSWORD in settings.postgres_dsn_override:
        raise ValueError(
            "RAVEL_POSTGRES_DSN contains the shipped development database "
            "password; the effective credential is the one in the DSN, so it "
            "has to be replaced even when the password field is not used"
        )

    if settings.s3_secret_key.get_secret_value() in {"", DEV_S3_SECRET}:
        raise ValueError(
            "RAVEL_S3_SECRET_KEY is unset or is the shipped development "
            "default; an artifact store reachable with a published secret is "
            "not storage"
        )

    if not _is_local_bind(settings.gateway_host) and not settings.gateway_allow_public_bind:
        raise ValueError(
            "RAVEL_GATEWAY_HOST is not a loopback, private, or link-local "
            "address; a production Gateway is reached through the reverse "
            "proxy on the same host. Set RAVEL_GATEWAY_ALLOW_PUBLIC_BIND=true "
            "only if the bind is deliberate"
        )

    if public_origin is None:
        raise ValueError(
            "RAVEL_GATEWAY_PUBLIC_ORIGIN is required in production; it is the "
            "HTTPS origin the reverse proxy serves and the origin a browser is "
            "allowed to present"
        )
    if not allowed:
        raise ValueError(
            "RAVEL_GATEWAY_ALLOWED_ORIGINS is required in production; the event "
            "stream refuses every browser when the allowlist is empty"
        )
    for origin in allowed:
        if urlsplit(origin).scheme != "https":
            raise ValueError(
                "RAVEL_GATEWAY_ALLOWED_ORIGINS may contain only https origins in "
                "production"
            )
    if not settings.gateway_trusted_proxies.strip():
        raise ValueError(
            "RAVEL_GATEWAY_TRUSTED_PROXIES is required in production; without "
            "it every client is charged to the reverse proxy, so one caller can "
            "lock every account out of the login endpoint"
        )


def normalize_origin(value: str) -> str | None:
    """A request's `Origin` header as the configured form, or `None`.

    `None` means the header is absent in every sense that matters: empty,
    malformed, or not a bare origin. The socket route refuses such a request
    whether or not a deployment configured an allowlist, because an origin
    that cannot be compared is an origin that has not been allowed.
    """
    try:
        return _origin(value, "Origin")
    except ValueError:
        return None


def _origin(value: str, variable: str) -> str:
    """One normalized origin, or a refusal naming the variable.

    Only the origin is accepted. `https://example.org/app` and
    `https://example.org?x=1` both look like an origin to a reader and are not
    one to a browser's `Origin` header, so accepting them would configure a
    value that can never match.
    """
    text = value.strip()
    if not text:
        raise ValueError(f"{variable} contains an empty origin")
    if "*" in text:
        raise ValueError(
            f"{variable} may not contain a wildcard; the stream compares an "
            "exact origin rather than a pattern"
        )
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError(f"{variable} has to name an http or https origin")
    if not parsed.hostname:
        raise ValueError(f"{variable} has to name a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{variable} may not carry credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(
            f"{variable} has to be an origin, not a URL with a path, query, or fragment"
        )
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{variable} has a port that is not a number") from error
    host = parsed.hostname.lower()
    if ":" in host:  # an IPv6 literal, which urlsplit reports without brackets
        host = f"[{host}]"
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    authority = host if port in {None, default_port} else f"{host}:{port}"
    return f"{parsed.scheme.lower()}://{authority}"


def _is_local_bind(host: str) -> bool:
    """Whether a bind address is one only this host or its network can reach.

    A hostname is not accepted as local. It would have to be resolved, and a
    name that resolves privately at startup may resolve publicly later; an
    operator who means a private name can say so through the override.
    """
    try:
        address = ipaddress.ip_address(host.strip().strip("[]"))
    except ValueError:
        return False
    if address.is_unspecified:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


__all__ = [
    "DEV_POSTGRES_PASSWORD",
    "DEV_S3_SECRET",
    "MAX_STREAM_REAUTHORIZE_SECONDS",
    "exact_origins",
    "normalize_origin",
    "parse_origins",
    "validate_gateway_settings",
]
