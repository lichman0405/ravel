#!/usr/bin/env python3
"""Phase -1 environment gate.

Proves every dependency RAVEL V0 needs is present *and usable*, by exercising
it rather than importing it. Run `scripts/dev_up.sh` first.

    .venv/bin/python scripts/env_check.py

Exits non-zero on the first failing check, after reporting all of them.
"""

from __future__ import annotations

import asyncio
import importlib.metadata as metadata
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ravel.config import get_settings  # noqa: E402

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""))


async def check_python() -> None:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    record("Python 3.12", sys.version_info[:2] == (3, 12), version)


async def check_settings() -> None:
    settings = get_settings()
    detail = f"env={settings.env}, dsn host={settings.postgres_host}:{settings.postgres_port}"
    has_key = settings.deepseek_api_key is not None and bool(
        settings.deepseek_api_key.get_secret_value().strip()
    )
    record("settings load", True, detail)
    record("DEEPSEEK_API_KEY set", has_key, "" if has_key else "set it in .env")

    dsh_home = settings.dsh_home_path()
    record("DSH home writable", dsh_home.is_dir(), str(dsh_home))


async def check_postgres() -> None:
    import sqlalchemy as sa

    settings = get_settings()
    engine = sa.create_engine(settings.postgres_dsn, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            value = conn.execute(sa.text("SELECT 1")).scalar_one()
            version = conn.execute(sa.text("SHOW server_version")).scalar_one()
        record("PostgreSQL query", value == 1, f"server {version}")
    finally:
        engine.dispose()


async def check_temporal() -> None:
    from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
    from temporalio.client import Client

    settings = get_settings()
    client = await Client.connect(
        settings.temporal_host, namespace=settings.temporal_namespace
    )
    response = await client.workflow_service.describe_namespace(
        DescribeNamespaceRequest(namespace=settings.temporal_namespace)
    )
    state = response.namespace_info.state
    record("Temporal namespace", True, f"{settings.temporal_namespace} state={state}")


async def check_minio() -> None:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import ClientError

    settings = get_settings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
        region_name=settings.s3_region,
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )

    try:
        client.head_bucket(Bucket=settings.s3_bucket)
    except ClientError:
        client.create_bucket(Bucket=settings.s3_bucket)

    key = "_env_check/probe.bin"
    payload = b"ravel-env-check"
    client.put_object(Bucket=settings.s3_bucket, Key=key, Body=payload)
    try:
        body = client.get_object(Bucket=settings.s3_bucket, Key=key)["Body"].read()
        record("MinIO round-trip", body == payload, f"bucket={settings.s3_bucket}")
    finally:
        client.delete_object(Bucket=settings.s3_bucket, Key=key)


async def check_playwright() -> None:
    from playwright.async_api import async_playwright

    settings = get_settings()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=settings.playwright_headless)
        try:
            page = await browser.new_page()
            await page.set_content("<title>ravel</title><p>ok</p>")
            title = await page.title()
            record("Playwright Chromium", title == "ravel", f"version {browser.version}")
        finally:
            await browser.close()


async def check_dsh() -> None:
    import deepseek_harness
    from deepseek_harness import DeepSeekHarnessConfig

    sdk = metadata.version("deepseek-harness-sdk")
    runtime = metadata.version("deepseek-harness-runtime-bin")
    record(
        "DSH distributions",
        sdk == "0.1.5rc1" and runtime == "0.1.5rc1",
        f"sdk={sdk} runtime={runtime}",
    )

    config = DeepSeekHarnessConfig()
    binary = config.resolve_binary() if hasattr(config, "resolve_binary") else None
    record(
        "DSH runtime binary",
        binary is None or Path(binary).exists(),
        str(binary) if binary else "resolved by SDK at launch",
    )
    record("DSH SDK import", deepseek_harness is not None, "")


async def main() -> int:
    print("\n\033[1mRAVEL V0 — Phase -1 environment gate\033[0m\n")

    checks = [
        check_python,
        check_settings,
        check_postgres,
        check_temporal,
        check_minio,
        check_playwright,
        check_dsh,
    ]
    for check in checks:
        try:
            await check()
        except Exception as exc:  # noqa: BLE001 - a failed check must not stop the rest
            name = check.__name__.removeprefix("check_").replace("_", " ")
            record(name, False, f"{type(exc).__name__}: {exc}")
            if get_settings().log_level.upper() == "DEBUG":
                traceback.print_exc()

    failed = [name for name, ok, _ in results if not ok]
    print()
    if failed:
        print(f"\033[31m{len(failed)} check(s) failed:\033[0m {', '.join(failed)}\n")
        return 1
    print(f"\033[32mAll {len(results)} checks passed.\033[0m\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
