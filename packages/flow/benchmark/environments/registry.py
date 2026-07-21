"""Typed catalog of the pinned system-of-record environments.

Digests are the single source of truth in each environment's
``environment.lock.json``; this registry *reads* them rather than restating them,
so the two can never drift. Everything the lock file does not carry — the
system-of-record channel an oracle reads, the loopback ports, where the generated
credentials live, the bring-up/teardown commands, and the licensing/packaging
boundary — is declared here.

Consumers:

- the **effect-oracle harness** uses :attr:`Environment.sor` to know which
  channel (SQL DSN, REST readback, HTTP-JSON) reads the true business effect,
  independently of the agent's action channel;
- the **task pack** uses :func:`all_environments` / :func:`get` to enumerate the
  substrates a task may target and to fetch reproducible bring-up commands;
- CI uses :func:`registry_snapshot` + :mod:`benchmark.environments.verify` to
  assert every environment stays digest-pinned and its record stays queryable.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ``benchmark/environments/registry.py`` -> ``benchmark`` -> repo root.
BENCHMARK_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BENCHMARK_ROOT.parent

# An OCI image reference is digest-pinned iff it ends in ``@sha256:<64 hex>``.
# A bare or floating tag (``mariadb:11.8``) is rejected: tags are mutable.
_DIGEST_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")


class RegistryError(RuntimeError):
    """A registry/lock invariant was violated; callers must not proceed."""


@dataclass(frozen=True)
class LicenseStatus:
    """The upstream license and how it constrains shipped package artifacts.

    The workspace hard rule: *running* a pinned upstream container is fine;
    *vendoring* copyleft source into an MIT/Apache wheel or sdist is not.
    ``ship_in_artifacts`` records whether any file of this environment may land
    in a built wheel/sdist. For every copyleft app it is ``False``.
    """

    upstream_license: str
    # How the upstream code reaches a runner: pulled at runtime (not vendored),
    # built at runtime from pinned upstream (not vendored), or adapted config
    # vendored repo-only under its own license (never shipped).
    delivery: str
    ship_in_artifacts: bool
    notes: str


@dataclass(frozen=True)
class SystemOfRecord:
    """How an oracle reads the true business effect, off-screen.

    ``channels`` are the independent read paths (``"sql"``, ``"rest_api"``,
    ``"http_json"``). ``read_recipe`` is a one-line description of the exact
    pre/post read an oracle performs. ``isolated_from_actor`` is ``True`` when
    the oracle's read credentials/path are provably separate from the write
    channel the agent drives (least-privilege REST client, read-only DB user,
    or an out-of-band API the UI never calls).
    """

    kind: str
    channels: tuple[str, ...]
    read_recipe: str
    reset_recipe: str
    isolated_from_actor: bool


@dataclass(frozen=True)
class Environment:
    """One pinned, reproducible application with an accessible record."""

    name: str
    app: str
    vertical: str
    substrate: str
    # Path (relative to the repo root) of the environment.lock.json, or ``None``
    # for the in-process MockMed anchor which pins nothing external.
    lock_relpath: str | None
    compose_relpath: str | None
    ci_fast: bool
    requires_docker: bool
    ui_urls: dict[str, str]
    sor: SystemOfRecord
    creds_location: str
    seed_state: str
    bringup: tuple[str, ...]
    teardown: tuple[str, ...]
    license: LicenseStatus
    reference_docs: tuple[str, ...] = field(default_factory=tuple)

    @property
    def lock_path(self) -> Path | None:
        return None if self.lock_relpath is None else REPO_ROOT / self.lock_relpath

    @property
    def compose_path(self) -> Path | None:
        return (
            None if self.compose_relpath is None else REPO_ROOT / self.compose_relpath
        )

    def load_lock(self) -> dict[str, Any]:
        """Return the parsed lock file (or ``{}`` for the anchor)."""
        path = self.lock_path
        if path is None:
            return {}
        if not path.is_file():
            raise RegistryError(f"{self.name}: missing lock file {self.lock_relpath}")
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError as exc:  # pragma: no cover - corruption path
            raise RegistryError(f"{self.name}: unparseable lock file") from exc

    def service_digests(self) -> dict[str, str]:
        """Return ``{service: pinned_image}`` from the lock (empty for anchor)."""
        services = self.load_lock().get("services", {})
        if not isinstance(services, dict):
            raise RegistryError(f"{self.name}: lock 'services' is not an object")
        return {str(k): str(v) for k, v in services.items()}

    def unpinned_services(self) -> dict[str, str]:
        """Return any service whose image is not ``@sha256:`` digest-pinned."""
        return {
            svc: img
            for svc, img in self.service_digests().items()
            if not _DIGEST_RE.match(img)
        }


# ---------------------------------------------------------------------------
# The catalog. Three named substrates the task asked for (EMR, ERP, CI anchor)
# plus the AGPL insurance mirror, kept opt-in / repo-only.
# ---------------------------------------------------------------------------

_MOCKMED = Environment(
    name="mockmed",
    app="MockMed fault-injection SPA (bundled)",
    vertical="fixture",
    substrate="web",
    lock_relpath=None,
    compose_relpath=None,
    ci_fast=True,
    requires_docker=False,
    ui_urls={"app": "http://127.0.0.1:<ephemeral>/"},
    sor=SystemOfRecord(
        kind="in-process encounter store behind a real HTTP persistence boundary",
        channels=("http_json",),
        read_recipe="GET /api/db -> {records:[{id,patient_id,type,note,source,key}]}",
        reset_recipe="POST /api/reset {seed_concurrent?: bool}",
        # The store is only reachable via the fault-server API; the SPA writes
        # through the same boundary but the oracle reads /api/db directly, a
        # path the rendered screen never surfaces.
        isolated_from_actor=True,
    ),
    creds_location="none (loopback, no auth)",
    seed_state=(
        "empty store; POST /api/reset {seed_concurrent:true} plants one "
        "concurrent-actor row to expose lost-update (C4/stale) faults"
    ),
    bringup=(
        'python -c "from openadapt_flow.mockmed.fault_server import serve; '
        'print(serve(port=0)[0])"',
        "# or: python -m benchmark.environments.verify mockmed",
    ),
    teardown=("call the returned stop() callable (daemon thread; no residue)",),
    license=LicenseStatus(
        upstream_license="MIT (OpenAdapt Flow's own code)",
        delivery="first-party source; shipped in the wheel as openadapt_flow.mockmed",
        ship_in_artifacts=True,
        notes="Fake data only; the CI-fast anchor that needs no Docker.",
    ),
    reference_docs=(
        "openadapt_flow/mockmed/fault_server.py",
        "benchmark/fault_model/FAULT_MODEL.md",
    ),
)

_OPENEMR = Environment(
    name="openemr_local",
    app="OpenEMR 8.0.0.3 (containerized EMR)",
    vertical="healthcare",
    substrate="web",
    lock_relpath="benchmark/openemr_local/environment.lock.json",
    compose_relpath="benchmark/openemr_local/compose.yml",
    ci_fast=False,
    requires_docker=True,
    ui_urls={
        "http_ui": "http://127.0.0.1:9301",
        "https_api": "https://127.0.0.1:9300",
    },
    sor=SystemOfRecord(
        kind="MariaDB row-state + OpenEMR Standard REST readback",
        channels=("sql", "rest_api"),
        read_recipe=(
            "SQL: MariaDB service 'db', SELECT from openemr.patient_data; "
            "REST: GET https://127.0.0.1:9300/apis/default/api/patient with a "
            "least-privilege OAuth oracle client (scope 'openid api:oemr "
            "user/patient.rs' -> read/search only, no create/update/delete)"
        ),
        reset_recipe=(
            "OpenEMRFixture.reset(): restore the SHA-256-verified baseline SQL "
            "dump while the openemr writer container is stopped"
        ),
        isolated_from_actor=True,
    ),
    creds_location=(
        "benchmark/openemr_local/state/runtime.env (generated, 0600, gitignored) "
        "and state/oauth-clients.json; actor user 'openadapt_actor'"
    ),
    seed_state=(
        "pinned image install + APIs enabled by bootstrap(); one immutable "
        "hashed SQL baseline (snapshot()) restored before every trial"
    ),
    bringup=(
        "python scripts/openemr_local_demo.py preflight",
        "python scripts/openemr_local_demo.py prepare",
        "python scripts/openemr_local_demo.py up",
        "python scripts/openemr_local_demo.py bootstrap",
        "python scripts/openemr_local_demo.py snapshot",
    ),
    teardown=(
        "docker compose -p openadapt-openemr-benchmark down -v",
        "rm -rf benchmark/openemr_local/state",
    ),
    license=LicenseStatus(
        upstream_license="GPL-3.0 (OpenEMR)",
        delivery=(
            "official openemr/openemr image pulled at runtime by digest; no "
            "OpenEMR source vendored into this repo"
        ),
        ship_in_artifacts=False,
        notes=(
            "Running a pinned upstream container is not redistribution. The "
            "compose.yml is OpenAdapt's own (adapted, attributed). Nothing "
            "OpenEMR-licensed is under openadapt_flow/, so the wheel excludes it."
        ),
    ),
    reference_docs=(
        "benchmark/openemr_local/README.md",
        "benchmark/openemr_local/fixture.py",
    ),
)

_FRAPPE = Environment(
    name="frappe_lending",
    app="Frappe/ERPNext + Lending v16 (containerized ERP)",
    vertical="lending",
    substrate="web",
    lock_relpath="benchmark/frappe_lending/environment.lock.json",
    compose_relpath="benchmark/frappe_lending/compose.yml",
    ci_fast=False,
    requires_docker=True,
    ui_urls={"http_ui": "http://127.0.0.1:8080"},
    sor=SystemOfRecord(
        kind="MariaDB row-state + Frappe REST readback",
        channels=("sql", "rest_api"),
        read_recipe=(
            "SQL: MariaDB service 'db', SELECT from `tabLoan Application`; "
            "REST: Frappe /api authenticated as read-only oracle user "
            "'openadapt.oracle@example.invalid' (custom read-only Loan "
            "Application permission; not the UI/API writer)"
        ),
        reset_recipe=(
            "FrappeLendingFixture.reset(): restore the SHA-256-verified baseline "
            "SQL dump with writer services stopped"
        ),
        isolated_from_actor=True,
    ),
    creds_location=(
        "benchmark/frappe_lending/state/runtime.env (generated, 0600, "
        "gitignored); site 'frontend', admin via SITE_ADMIN_PASSWORD"
    ),
    seed_state=(
        "custom image built from pinned upstream tags (verified against locked "
        "commits); one immutable hashed SQL baseline restored before every trial"
    ),
    bringup=(
        "python scripts/frappe_lending_demo.py preflight",
        "python scripts/frappe_lending_demo.py prepare",
        "python scripts/frappe_lending_demo.py build",
        "python scripts/frappe_lending_demo.py up",
        "python scripts/frappe_lending_demo.py bootstrap",
        "python scripts/frappe_lending_demo.py snapshot",
    ),
    teardown=(
        "docker compose -p openadapt-frappe-lending-benchmark down -v",
        "rm -rf benchmark/frappe_lending/state",
    ),
    license=LicenseStatus(
        upstream_license="GPL-3.0 (Frappe/ERPNext/Lending), MIT (frappe_docker)",
        delivery=(
            "custom image built at runtime from official frappe/build+base "
            "images and pinned app tags; no Frappe source vendored into this repo"
        ),
        ship_in_artifacts=False,
        notes=(
            "Build+run only, from digest-pinned bases and commit-pinned apps. "
            "Top-level benchmark/ is outside the openadapt_flow wheel package."
        ),
    ),
    reference_docs=(
        "benchmark/frappe_lending/README.md",
        "benchmark/frappe_lending/fixture.py",
    ),
)

_OPENIMIS = Environment(
    name="openimis_claims",
    app="openIMIS 25.10 (containerized insurance claims)",
    vertical="insurance",
    substrate="web",
    lock_relpath="benchmark/openimis_claims/environment.lock.json",
    compose_relpath="benchmark/openimis_claims/compose.yml",
    ci_fast=False,
    requires_docker=True,
    ui_urls={"http_ui": "http://127.0.0.1:9401"},
    sor=SystemOfRecord(
        kind="PostgreSQL row-state (tblClaim)",
        channels=("sql",),
        read_recipe=(
            "SQL: PostgreSQL 'IMIS' database; require exactly one non-voided "
            "tblClaim row with the trial claim code in status 2 ('Entered') for "
            "the synthetic insuree + health facility"
        ),
        reset_recipe="compose down --volumes (full reset) then up + bootstrap",
        isolated_from_actor=True,
    ),
    creds_location=(
        "benchmark/openimis_claims/out/state/ (generated, gitignored); demo "
        "actor credential 'Admin'"
    ),
    seed_state=(
        "upstream openIMIS demo dataset (fictional regions/facilities/insurees) "
        "plus one synthetic policyholder added by bootstrap()"
    ),
    bringup=(
        "python scripts/openimis_claims_demo.py up",
        "python scripts/openimis_claims_demo.py bootstrap",
    ),
    teardown=("python scripts/openimis_claims_demo.py down --volumes",),
    license=LicenseStatus(
        upstream_license="AGPL-3.0-only (openIMIS)",
        delivery=(
            "OPT-IN / REPO-ONLY: images pulled at runtime by digest; the adapted "
            "compose.yml + conf/nginx templates are AGPL-3.0-only, carry SPDX "
            "headers, and are recorded in THIRD_PARTY_NOTICES.md"
        ),
        ship_in_artifacts=False,
        notes=(
            "CAVEAT — enforced by pyproject sdist 'exclude': /benchmark/"
            "openimis_claims, /scripts/openimis_claims_demo.py, "
            "/tests/test_openimis_claims_fixture.py, /THIRD_PARTY_NOTICES.md are "
            "kept out of the sdist; the wheel packages only openadapt_flow/, so "
            "no AGPL file can ship in either artifact. Do NOT relocate these "
            "files under openadapt_flow/."
        ),
    ),
    reference_docs=(
        "benchmark/openimis_claims/README.md",
        "THIRD_PARTY_NOTICES.md",
    ),
)

ENVIRONMENTS: tuple[Environment, ...] = (
    _MOCKMED,
    _OPENEMR,
    _FRAPPE,
    _OPENIMIS,
)


def all_environments() -> tuple[Environment, ...]:
    """Return every registered environment."""
    return ENVIRONMENTS


def get(name: str) -> Environment:
    """Return the environment with ``name`` or raise ``KeyError``."""
    for env in ENVIRONMENTS:
        if env.name == name:
            return env
    raise KeyError(name)


def registry_snapshot() -> dict[str, Any]:
    """Return a JSON-serializable snapshot with digests resolved from locks.

    This is what non-Python consumers (and the committed ``environments.json``
    evidence) read. Digests come from the live lock files, so the snapshot can
    never claim a pin the lock file does not actually hold.
    """
    envs: list[dict[str, Any]] = []
    for env in ENVIRONMENTS:
        row = asdict(env)
        # Drop the Path-derived properties (asdict only sees fields) and attach
        # the resolved, live digests instead.
        row["service_digests"] = env.service_digests()
        row["unpinned_services"] = env.unpinned_services()
        envs.append(row)
    return {
        "schema_version": 1,
        "description": (
            "Pinned, reproducible system-of-record environments for the Silent "
            "Wrong-Effect benchmark. Digests are resolved from each "
            "environment.lock.json at snapshot time."
        ),
        "environments": envs,
    }
