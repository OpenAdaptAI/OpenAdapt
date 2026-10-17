# SPDX-License-Identifier: MIT
"""Build the openIMIS compose project from the pinned upstream distribution.

The openIMIS Docker distribution (``openimis/openimis-dist_dkr``) is licensed
AGPL-3.0-only, so none of its files are stored in this repository. At run
time ``prepare_compose`` fetches the exact files listed under
``upstreams.openimis-dist_dkr.files`` in ``environment.lock.json`` at the
pinned commit, checks each SHA-256, and caches them under ``out/upstream``
(git-ignored). It then derives the local compose file from the upstream
compose files with the transformation below and writes it next to the cache.

The transformation keeps the upstream services the claims-intake reference
workflow needs (db, migrations, backend, worker, frontend, redis, rabbitmq)
and drops OpenSearch, certbot, Traefik labels and the custom network. Every
image comes from a required, digest-pinned ``*_IMAGE`` variable, every
published port binds to loopback, and secrets come from required variables
the fixture generates. The nginx templates are mounted unmodified from the
verified cache.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .pinned_fetch import UpstreamFetchError, cache_root, fetch_pinned

HERE = Path(__file__).resolve().parent
UPSTREAM_KEY = "openimis-dist_dkr"
REPO = "openimis/openimis-dist_dkr"
NGINX_DIR = "conf/nginx"

__all__ = ["UpstreamFetchError", "prepare_compose", "render_compose"]


def _req(var: str, what: str) -> str:
    return "${" + var + ":?" + what + "}"


SECRET = "generated fixture secret required"

# Values for the variables the upstream compose files leave to the operator.
API_ENV: dict[str, str] = {
    "DB_DEFAULT": "postgresql",
    "DB_HOST": "db",
    "DB_PORT": "5432",
    "DB_NAME": "IMIS",
    "DB_USER": "IMISuser",
    "DB_PASSWORD": _req("OPENIMIS_DB_PASSWORD", SECRET),
    "SECRET_KEY": _req("OPENIMIS_SECRET_KEY", SECRET),
    "SITE_URL": "localhost",
    "MODE": "Prod",
    "HOSTS": "localhost",
    "CACHE_BACKEND": "django.core.cache.backends.redis.RedisCache",
    "CACHE_URL": "redis://:" + _req("OPENIMIS_REDIS_PASSWORD", SECRET) + "@redis:6379",
    "CACHE_OPTIONS": "{}",
}
API_ENV_EXTRA: dict[str, str] = {
    "CSRF_TRUSTED_ORIGINS": "http://localhost:${OPENIMIS_HTTP_PORT:-9401}",
    "DJANGO_LOG_LEVEL": "WARNING",
    "DJANGO_MIGRATE": "True",
}
DB_ENV: dict[str, str] = {
    "POSTGRES_PASSWORD": _req("OPENIMIS_DB_PASSWORD", SECRET),
    "POSTGRES_DB": "IMIS",
    "POSTGRES_USER": "IMISuser",
    # The upstream demo dataset is synthetic (fictional regions, insurees,
    # and tariffs); it is the baseline the reference environment records on.
    "DEMO_DATASET": "true",
}
FE_ENV: dict[str, str] = {
    "REACT_APP_API_URL": "api",
    "NEW_OPENIMIS_HOST": "localhost",
    "REACT_APP_SENTRY_DSN": "",
}
DROP_ENV_PREFIXES = ("OPENSEARCH_", "OPENIMIS_CONF_JSON")
IMAGES = {
    "migrations": "OPENIMIS_BE_IMAGE",
    "backend": "OPENIMIS_BE_IMAGE",
    "worker": "OPENIMIS_BE_IMAGE",
    "frontend": "OPENIMIS_FE_IMAGE",
    "db": "OPENIMIS_PGSQL_IMAGE",
    "redis": "OPENIMIS_REDIS_IMAGE",
    "rabbitmq": "OPENIMIS_RABBITMQ_IMAGE",
}
KEEP = tuple(IMAGES)


def _env_list(
    upstream: Any, values: Mapping[str, str], extra: Mapping[str, str] = {}
) -> list[str]:
    """Fill the upstream variable names with local values, in upstream order."""

    pairs = [str(item).split("=", 1) for item in (upstream or [])]
    names = [pair[0] for pair in pairs]
    out: list[str] = []
    for name, *rest in pairs:
        if name.startswith(DROP_ENV_PREFIXES):
            continue
        if name in values:
            out.append(f"{name}={values[name]}")
        elif rest and "$" not in rest[0]:
            out.append(f"{name}={rest[0]}")  # upstream literal, kept as is
        else:
            raise UpstreamFetchError(
                f"upstream compose sets {name}, which has no local value"
            )
    missing = set(values) - set(names)
    if missing:
        raise UpstreamFetchError(f"upstream compose no longer sets {sorted(missing)}")
    out.extend(f"{k}={v}" for k, v in extra.items())
    return out


def render_compose(upstream_root: Path) -> dict[str, Any]:
    """Return the local compose document derived from the verified upstream files."""

    merged: dict[str, Any] = {}
    for name in ("compose.base.yml", "compose.postgresql.yml", "compose.cache.yml"):
        doc = yaml.safe_load((upstream_root / name).read_text())
        merged.update({k: v for k, v in doc.items() if k != "services"})
        merged.setdefault("services", {}).update(doc.get("services", {}))
    services: dict[str, Any] = merged["services"]
    missing = [name for name in KEEP if name not in services]
    if missing:
        raise UpstreamFetchError(f"upstream compose lacks services {missing}")

    out: dict[str, Any] = {}
    for name in KEEP:
        svc = dict(services[name])
        for key in ("networks", "labels", "env_file", "deploy"):
            svc.pop(key, None)
        svc["image"] = _req(IMAGES[name], f"pinned {IMAGES[name]} required")
        if svc.get("restart") == "always":
            svc["restart"] = "unless-stopped"
        if name in ("migrations", "backend", "worker"):
            svc["environment"] = _env_list(
                svc.get("environment"), API_ENV, API_ENV_EXTRA
            )
            if name == "migrations":
                svc["restart"] = "no"
        elif name == "db":
            svc["environment"] = _env_list(svc.get("environment"), DB_ENV)
            svc["healthcheck"] = {
                **svc["healthcheck"],
                "test": "pg_isready -U IMISuser -d IMIS",
            }
            svc.pop("restart", None)
            # Loopback only: the read-only SQL oracle reads from the host.
            svc["ports"] = ["127.0.0.1:${OPENIMIS_DB_PORT:-9402}:5432"]
        elif name == "frontend":
            svc["environment"] = _env_list(svc.get("environment"), FE_ENV)
            svc["ports"] = ["127.0.0.1:${OPENIMIS_HTTP_PORT:-9401}:80"]
            # The nginx templates, unmodified, from the verified cache.
            svc["volumes"] = [f"{(upstream_root / NGINX_DIR).as_posix()}:/conf/"]
        elif name == "redis":
            svc["command"] = "redis-server --requirepass " + _req(
                "OPENIMIS_REDIS_PASSWORD", SECRET
            )
            svc.pop("ports", None)
            svc.pop("volumes", None)
            svc["environment"] = [
                "REDIS_PASSWORD=" + _req("OPENIMIS_REDIS_PASSWORD", SECRET)
            ]
            svc["healthcheck"] = {
                **svc["healthcheck"],
                "test": [
                    "CMD-SHELL",
                    'redis-cli -a "$$REDIS_PASSWORD" ping | grep PONG',
                ],
            }
        out[name] = svc
    return {"services": out, "volumes": {"database": None, "photos": None}}


def upstream_files(lock: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    upstream = lock["upstreams"][UPSTREAM_KEY]
    return upstream["commit"], dict(upstream["files"])


def prepare_compose(
    lock: Mapping[str, Any], *, out_dir: Path | None = None, opener=None
) -> Path:
    """Fetch and verify the upstream files, then write the local compose file."""

    commit, files = upstream_files(lock)
    base = cache_root((out_dir or HERE / "out") / "upstream")
    kwargs = {"opener": opener} if opener is not None else {}
    root = fetch_pinned(REPO, commit, files, base, **kwargs)
    doc = render_compose(root)
    target = root.parent / f"compose.{commit[:12]}.yml"
    header = (
        "# Generated by benchmark/openimis_claims/upstream.py from openimis-dist_dkr\n"
        f"# {commit} (AGPL-3.0-only). Local use only; do not commit.\n"
    )
    target.write_text(header + yaml.safe_dump(doc, sort_keys=False))
    return target


if __name__ == "__main__":  # pragma: no cover - manual helper
    print(prepare_compose(json.loads((HERE / "environment.lock.json").read_text())))
