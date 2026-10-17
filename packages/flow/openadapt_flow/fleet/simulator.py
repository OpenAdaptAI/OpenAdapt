"""A local fleet simulator: three tenants, one payer portal, one UI change.

Everything runs in one process with no network. Each simulated tenant has its
own repair store, bundle, qualification cases, synthetic member data, trust
pins, and promotion policy. Patches move through a :class:`FilePatchFeed`
directory. Run it with::

    python -m openadapt_flow.fleet.simulator --out /tmp/fleet-sim

What is real and what is simulated:

* **Real:** the Flow :class:`~openadapt_flow.runtime.replayer.Replayer`, the
  Standard execution profile run gate, independent effect verification
  (``RestRecordVerifier`` reading the simulated system of record), the
  identity gate, the repair lifecycle store, the patch format, signing,
  import, canary, and policy code.
* **Simulated:** the application (a structural backend over a list of
  elements per portal version), its screen text (a deterministic OCR stand-in
  that reads the simulated screen, not a model), and its system of record.
  The canary replays in process, as the tutorial does; a production canary
  is the tenant's qualification run path behind the same
  :class:`~openadapt_flow.fleet.canary.TenantCanaryRunner` interface.

The scenario: the portal moves from 7.1 to 7.2 and renames its controls.
Tenant ``north`` halts, a person repairs and approves it, and ``north``
exports a signed patch. Tenant ``east`` imports it, its canary passes on
``east``'s own cases, and its policy auto-promotes. Tenant ``west`` is still
on 7.1, so the patch is refused; after ``west`` upgrades it imports, the
canary passes, and the candidate waits for a person because ``west``'s policy
does not allow auto-promotion.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Optional, Sequence

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PIL import Image

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.fleet.canary import QualificationCase, canary_patch
from openadapt_flow.fleet.export import export_patch
from openadapt_flow.fleet.feed import FilePatchFeed
from openadapt_flow.fleet.fingerprint import (
    AppVersionFingerprint,
    fingerprint_from_observation,
)
from openadapt_flow.fleet.intake import PatchImportError, import_patch
from openadapt_flow.fleet.patch import SignedStructuralPatch
from openadapt_flow.fleet.policy import (
    FleetPromotionPolicy,
    PromotionDecision,
    process_imported_patch,
)
from openadapt_flow.fleet.signing import (
    FleetTrust,
    SignerCertificate,
    issue_certificate,
    public_key_b64,
)
from openadapt_flow.ir import (
    ActionDeliveryReceipt,
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    RunReport,
    Step,
    StructuralHandle,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.qualification_environment import (
    QualificationEnvironmentObservation,
)
from openadapt_flow.repair.candidate import Actor
from openadapt_flow.repair.lifecycle import RepairStore, observation_from_report
from openadapt_flow.repair.registration import build_candidate

APP_FAMILY = "sim-payer/portal"
WORKFLOW_FAMILY = "sim-payer/eligibility-check"
VIEWPORT = (400, 240)
ROW_Y = 120
#: Text on the search control's row: that step's identity is the portal
#: itself, not a member.
PORTAL_BANNER = "Sim Payer eligibility portal"

# --------------------------------------------------------------------------- #
# The simulated application
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Element:
    selector: str
    role: str
    name: str
    rect: tuple[int, int, int, int]
    #: What clicking it does: "search" shows the member row, "submit" writes
    #: the eligibility request to the system of record.
    does: str


#: Each portal version is the elements on its one page. 7.2 renames both
#: controls and moves the submit button.
PORTAL_VERSIONS: dict[str, tuple[Element, ...]] = {
    "7.1": (
        Element("#member-search", "button", "Search", (20, 40, 80, 24), "search"),
        Element(
            "#submit-check", "button", "Check eligibility", (300, 112, 80, 24), "submit"
        ),
    ),
    "7.2": (
        Element("#find-member", "button", "Search", (20, 40, 80, 24), "search"),
        Element(
            "[data-testid=run-eligibility]",
            "button",
            "Check eligibility",
            (260, 112, 100, 24),
            "submit",
        ),
    ),
}


def portal_html(version: str, *, member_rows: int = 1) -> str:
    """The page markup for one version. Text differs per tenant; the
    structure does not."""
    rows = "".join(
        f'<tr class="member"><td>Member {i}</td><td>DOB 1970-01-0{i % 9 + 1}</td></tr>'
        for i in range(member_rows)
    )
    if version == "7.1":
        return (
            "<html><body><div class='portal'><button id='member-search'>Search"
            f"</button><table>{rows}</table><button id='submit-check'>Check "
            "eligibility</button></div></body></html>"
        )
    return (
        "<html><body><main class='portal'><section><button id='find-member' "
        f"type='button'>Search</button></section><table>{rows}</table>"
        "<section><button data-testid='run-eligibility' type='button'>Check "
        "eligibility</button></section></main></body></html>"
    )


def app_fingerprint(version: str, *, member_rows: int = 1) -> AppVersionFingerprint:
    observation = QualificationEnvironmentObservation(
        target_kind="web",
        application_identity="https://portal.sim-payer.example",
        application_version=version,
        session_identity_sha256="0" * 64,
        environment_digest="0" * 64,
    )
    return fingerprint_from_observation(
        observation,
        app_family=APP_FAMILY,
        html=portal_html(version, member_rows=member_rows),
    )


class SystemOfRecord:
    def __init__(self) -> None:
        self.records: list[dict[str, str]] = []


class _Response:
    status_code = 200

    def __init__(self, body: Any) -> None:
        self._body = body

    def json(self) -> Any:
        return self._body


class _RecordSession:
    """The ``requests``-style session ``RestRecordVerifier`` reads through."""

    def __init__(self, sor: SystemOfRecord) -> None:
        self.sor = sor

    def get(self, url: str, timeout: Any = None, headers: Any = None) -> _Response:
        return _Response({"records": [dict(r) for r in self.sor.records]})


def _blank_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", VIEWPORT, (250, 250, 250)).save(buffer, format="PNG")
    return buffer.getvalue()


class SimulatedPortal:
    """A structural backend for one portal version and one member on screen.

    ``fault`` presents a qualification fault: ``wrong_entity`` shows another
    member, ``duplicate_target`` renders the submit control twice, and
    ``missing_control`` removes it.
    """

    def __init__(
        self,
        version: str,
        *,
        member_band: str,
        member_key: str,
        idempotency_key: str,
        sor: SystemOfRecord,
        fault: Optional[str] = None,
    ) -> None:
        elements = list(PORTAL_VERSIONS[version])
        if fault == "duplicate_target":
            submit = next(e for e in elements if e.does == "submit")
            elements.append(
                Element(
                    submit.selector,
                    submit.role,
                    submit.name,
                    (20, 180, 80, 24),
                    "submit",
                )
            )
        if fault == "missing_control":
            elements = [e for e in elements if e.does != "submit"]
        self.elements = elements
        self.member_band = (
            "Member Other Person DOB 1999-12-31"
            if fault == "wrong_entity"
            else member_band
        )
        self.member_key = member_key
        self.idempotency_key = idempotency_key
        self.sor = sor
        self.searched = False
        self.submitted = False
        self.actions: list[tuple[Any, ...]] = []
        self._frame = _blank_png()
        self._guarded: Optional[tuple[int, int]] = None

    # -- screen text, read by SimulatedScreenReader ------------------------

    def screen_lines(self) -> list[tuple[str, tuple[int, int, int, int]]]:
        lines = [
            ("Member search", (20, 10, 120, 14)),
            (PORTAL_BANNER, (120, 46, 200, 14)),
        ]
        if self.searched:
            lines.append((self.member_band, (10, ROW_Y - 6, 220, 14)))
        if self.submitted:
            lines.append(("Eligibility request sent", (20, 200, 200, 14)))
        return lines

    # -- Backend -------------------------------------------------------------

    @property
    def viewport(self) -> tuple[int, int]:
        return VIEWPORT

    def screenshot(self) -> bytes:
        return self._frame

    def _hit(self, x: int, y: int) -> Optional[Element]:
        for element in self.elements:
            ex, ey, ew, eh = element.rect
            if ex <= x < ex + ew and ey <= y < ey + eh:
                return element
        return None

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self.actions.append(("click", x, y))
        element = self._hit(int(x), int(y))
        if element is None:
            return
        if element.does == "search":
            self.searched = True
        elif element.does == "submit" and self.searched:
            self.submitted = True
            self.sor.records.append(
                {"member": self.member_key, "key": self.idempotency_key}
            )

    def type_text(self, text: str) -> None:
        self.actions.append(("type", len(text)))

    def press(self, key: str) -> None:
        self.actions.append(("press", key))

    def scroll(self, dx: int, dy: int) -> None:
        self.actions.append(("scroll", dx, dy))

    def text_value_at(self, x: int, y: int) -> Optional[str]:
        return None

    # -- structural actuation -----------------------------------------------

    def structural_locator_at(self, x: int, y: int) -> Optional[StructuralLocator]:
        element = self._hit(x, y)
        if element is None:
            return None
        return StructuralLocator(
            selector=element.selector, role=element.role, name=element.name
        )

    @staticmethod
    def _fingerprint(element: Element) -> str:
        return hashlib.sha256(
            repr((element.selector, element.rect)).encode()
        ).hexdigest()

    def locate_structural(
        self, locator: StructuralLocator
    ) -> Optional[StructuralHandle]:
        if locator.selector is None:
            return None
        hits = [e for e in self.elements if e.selector == locator.selector]
        if not hits:
            return None
        if len(hits) > 1:
            raise StructuralResolutionRefused("more than one element matches")
        x, y, w, h = hits[0].rect
        return StructuralHandle(
            point=(x + w // 2, y + h // 2),
            region=(x, y, w, h),
            target_fingerprint=self._fingerprint(hits[0]),
        )

    def act_structural(
        self,
        locator: StructuralLocator,
        handle: StructuralHandle,
        *,
        double: bool = False,
    ) -> ActionDeliveryReceipt:
        fresh = self.locate_structural(locator)
        if fresh is None or fresh.target_fingerprint != handle.target_fingerprint:
            raise RuntimeError("the structural target changed before delivery")
        self.click(*fresh.point, double=double)
        return ActionDeliveryReceipt(
            receipt_id="sim-dom-click",
            operation="dom_click",
            native=False,
            target_fingerprint=fresh.target_fingerprint,
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )

    # -- guarded coordinate delivery (visual rungs; unused when structural wins)

    def arm_guarded_coordinate(self, x: int, y: int) -> None:
        self._guarded = (int(x), int(y))

    def cancel_guarded_coordinate(self) -> None:
        self._guarded = None

    def act_guarded_coordinate(
        self,
        x: int,
        y: int,
        *,
        expected_frame_sha256: str,
        double: bool = False,
        button: str = "left",
    ) -> ActionDeliveryReceipt:
        if self._guarded != (int(x), int(y)):
            raise RuntimeError("guarded coordinate target was not armed")
        if hashlib.sha256(self._frame).hexdigest() != expected_frame_sha256:
            raise RuntimeError("guarded coordinate frame changed")
        self._guarded = None
        self.click(x, y, double=double)
        return ActionDeliveryReceipt(
            receipt_id="sim-guarded-click",
            operation="guarded_coordinate_click",
            native=False,
            delivered_at=datetime.now(timezone.utc).isoformat(),
        )


class SimulatedScreenReader:
    """Deterministic stand-in for OCR and template matching.

    It reads :meth:`SimulatedPortal.screen_lines`. It is not a model and is
    never counted as one. Template matching always misses, so a step whose
    structural anchor no longer resolves halts instead of falling back to a
    pixel match.
    """

    def __init__(self, portal: SimulatedPortal) -> None:
        self.portal = portal

    def program_predicate_contract(self) -> dict[str, Any]:
        return {"present_texts": sorted(t for t, _ in self.portal.screen_lines())}

    def find_template(self, *args: Any, **kwargs: Any) -> None:
        return None

    def find_structural_template(self, *args: Any, **kwargs: Any) -> None:
        return None

    def find_text(self, screen_png: bytes, text: str, **kwargs: Any) -> Any:
        for line, region in self.portal.screen_lines():
            if text == line:
                x, y, w, h = region
                return SimpleNamespace(
                    point=(x + w // 2, y + h // 2), region=region, confidence=0.99
                )
        return None

    def text_present(self, screen_png: bytes, text: str, **kwargs: Any) -> bool:
        return self.find_text(screen_png, text) is not None

    def ocr(self, screen_png: bytes, *, region: Any = None) -> list[Any]:
        from openadapt_flow.vision.ocr import OcrLine

        lines = self.portal.screen_lines()
        if region is not None:
            _, top, _, height = region
            lines = [
                (line, box)
                for line, box in lines
                if box[1] < top + height and top < box[1] + box[3]
            ]
        return [OcrLine(text=line, region=box, confidence=0.99) for line, box in lines]

    def pixels_changed(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def phash_png(self, png: bytes, region: Any = None) -> str:
        return "aa"

    def phash_distance(self, a: str, b: str) -> int:
        return 0

    def wait_settled(self, backend: Any, **kwargs: Any) -> bytes:
        return backend.screenshot()

    def wait_settled_result(self, backend: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(png=backend.screenshot(), settled=True)


# --------------------------------------------------------------------------- #
# Tenants
# --------------------------------------------------------------------------- #

FAULT_KINDS = ("wrong_entity", "duplicate_target", "missing_control")


def build_bundle(
    path: Path, *, version: str, member_band: str, member_key: str
) -> Path:
    """Compile-equivalent bundle for one tenant on one portal version."""
    from openadapt_flow.runtime.effects import Effect, EffectKind
    from openadapt_flow.runtime.effects.effect import ValueExpr

    elements = {e.does: e for e in PORTAL_VERSIONS[version]}
    search, submit = elements["search"], elements["submit"]

    def anchor(element: Element, *, band: Optional[str]) -> Anchor:
        x, y, w, h = element.rect
        return Anchor(
            template=f"templates/{element.does}.png",
            region=element.rect,
            click_point=(x + w // 2, y + h // 2),
            ocr_text=element.name,
            context_text=band,
            structural=StructuralLocator(
                selector=element.selector, role=element.role, name=element.name
            ),
        )

    workflow = Workflow(
        name="eligibility-check",
        viewport=VIEWPORT,
        steps=[
            Step(
                id="search",
                intent="Find the member",
                action=ActionKind.CLICK,
                anchor=anchor(search, band=PORTAL_BANNER),
                identity_armed=True,
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT, text="Member search"
                    )
                ],
            ),
            Step(
                id="submit",
                intent="Send the eligibility request",
                action=ActionKind.CLICK,
                anchor=anchor(submit, band=member_band),
                identity_armed=True,
                risk="irreversible",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Eligibility request sent",
                    )
                ],
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"member": ValueExpr(literal=member_key)},
                        idempotency_key=ValueExpr(literal="eligibility-run"),
                        risk="irreversible",
                        timeout_s=0.5,
                    )
                ],
            ),
        ],
    )
    path.mkdir(parents=True, exist_ok=True)
    (path / "templates").mkdir(exist_ok=True)
    for element in (search, submit):
        buffer = io.BytesIO()
        Image.new("RGB", element.rect[2:], (40, 90, 200)).save(buffer, format="PNG")
        (path / "templates" / f"{element.does}.png").write_bytes(buffer.getvalue())
    workflow.save(path)
    return path


@dataclass
class SimulatedTenant:
    name: str
    root: Path
    portal_version: str
    member_band: str
    member_key: str
    policy: FleetPromotionPolicy
    trust: FleetTrust
    signer_key: Ed25519PrivateKey = field(  # gitleaks:allow (type annotation)
        default_factory=Ed25519PrivateKey.generate
    )
    certificate: Optional[SignerCertificate] = None
    model_calls: int = 0
    healthy_cases: int = 3
    fault_kinds: tuple[str, ...] = FAULT_KINDS

    @property
    def store(self) -> RepairStore:
        return RepairStore(self.root / "repair-store")

    @property
    def bundle(self) -> Path:
        return self.root / "bundles" / "active"

    def app(self) -> AppVersionFingerprint:
        return app_fingerprint(self.portal_version, member_rows=3)

    def cases(self) -> list[QualificationCase]:
        healthy = [
            QualificationCase(case_id=f"{self.name}-healthy-{i}", kind="healthy")
            for i in range(self.healthy_cases)
        ]
        faults = [
            QualificationCase(case_id=f"{self.name}-{fault}", kind="fault", fault=fault)
            for fault in self.fault_kinds
        ]
        return healthy + faults

    # -- TenantCanaryRunner ---------------------------------------------------

    def run_case(
        self, bundle_dir: Path, case: QualificationCase, run_dir: Path
    ) -> RunReport:
        return self.replay(bundle_dir, run_dir, fault=case.fault)

    def replay(
        self, bundle_dir: Path, run_dir: Path, *, fault: Optional[str] = None
    ) -> RunReport:
        """One Standard-profile run of ``bundle_dir`` on this tenant's portal."""
        from openadapt_flow.deployment import DeploymentConfig, PolicySection
        from openadapt_flow.execution_profiles import (
            ExecutionProfile,
            execution_profile_contract,
        )
        from openadapt_flow.run_gate import (
            build_runtime_authorization,
            evaluate_run_gate,
        )
        from openadapt_flow.runtime.effects import RestRecordVerifier
        from openadapt_flow.runtime.replayer import Replayer

        sor = SystemOfRecord()
        portal = SimulatedPortal(
            self.portal_version,
            member_band=self.member_band,
            member_key=self.member_key,
            idempotency_key="eligibility-run",
            sor=sor,
            fault=fault,
        )
        workflow = Workflow.load(bundle_dir)
        verifier = RestRecordVerifier(
            "http://sor.sim", session=_RecordSession(sor), poll_interval_s=0.01
        )
        gate = evaluate_run_gate(
            workflow,
            bundle_dir=bundle_dir,
            deployment=DeploymentConfig(policy=PolicySection(policy="permissive")),
            effect_verifier=verifier,
            profile_contract=execution_profile_contract(ExecutionProfile.STANDARD),
            effective_durable=True,
            effective_require_settled=True,
        )
        if not gate.passed:
            raise RuntimeError("run gate refused the bundle:\n" + gate.render())
        replayer = Replayer(
            portal,
            vision=SimulatedScreenReader(portal),
            use_structural=True,
            effect_verifier=verifier,
            governed_authorization=build_runtime_authorization(workflow, gate),
            durable=True,
            require_settled=True,
            poll_interval_s=0.01,
        )
        report = replayer.run(workflow, bundle_dir=bundle_dir, run_dir=run_dir)
        self.model_calls += report.model_calls
        return report


ROOT_KEY_VALIDITY = timedelta(days=30)


@dataclass
class Fleet:
    root: Path
    root_key: Ed25519PrivateKey  # gitleaks:allow (type annotation)
    feed: FilePatchFeed
    tenants: dict[str, SimulatedTenant] = field(default_factory=dict)

    @property
    def trust(self) -> FleetTrust:
        return FleetTrust.pin(self.root_key)

    def certify(self, tenant: SimulatedTenant, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        tenant.certificate = issue_certificate(
            self.root_key,
            tenant_pseudonym=hashlib.sha256(tenant.name.encode()).hexdigest()[:12]
            + "-tenant",
            signer_public_key=public_key_b64(tenant.signer_key),
            app_families=(APP_FAMILY,),
            not_before=now - timedelta(minutes=1),
            not_after=now + ROOT_KEY_VALIDITY,
        )

    def add_tenant(
        self,
        name: str,
        *,
        member_band: str,
        member_key: str,
        auto_promote: bool,
        portal_version: str = "7.1",
        healthy_cases: int = 3,
        fault_kinds: tuple[str, ...] = FAULT_KINDS,
        app_families: tuple[str, ...] = (APP_FAMILY,),
    ) -> SimulatedTenant:
        """A tenant with its own bundle, data, key, trust pins and policy."""
        now = datetime.now(timezone.utc)
        policy = FleetPromotionPolicy(
            approved_by=f"{name}-compliance-officer",
            approved_at=now.isoformat(),
            auto_promote=auto_promote,
            app_families=app_families,
            min_healthy_cases=healthy_cases,
            min_fault_cases=len(fault_kinds),
            canary_max_runs=2,
        )
        tenant = SimulatedTenant(
            name=name,
            root=self.root / name,
            portal_version=portal_version,
            member_band=member_band,
            member_key=member_key,
            policy=policy,
            trust=self.trust,
            healthy_cases=healthy_cases,
            fault_kinds=fault_kinds,
        )
        build_bundle(
            tenant.bundle,
            version=portal_version,
            member_band=member_band,
            member_key=member_key,
        )
        self.certify(tenant, now)
        self.tenants[name] = tenant
        return tenant


def build_fleet(root: Path) -> Fleet:
    """Three tenants on portal 7.1 with their own data, keys and policies."""
    fleet = Fleet(
        root=root,
        root_key=Ed25519PrivateKey.generate(),
        feed=FilePatchFeed(root / "feed"),
    )
    fleet.add_tenant(
        "north",
        member_band="Member Ada Lovelace DOB 1985-12-10",
        member_key="north-m-1",
        auto_promote=True,
    )
    fleet.add_tenant(
        "east",
        member_band="Member Grace Hopper DOB 1906-12-09",
        member_key="east-m-7",
        auto_promote=True,
    )
    fleet.add_tenant(
        "west",
        member_band="Member Alan Turing DOB 1912-06-23",
        member_key="west-m-3",
        auto_promote=False,
    )
    return fleet


# --------------------------------------------------------------------------- #
# Scenario steps
# --------------------------------------------------------------------------- #


def local_repair(
    tenant: SimulatedTenant,
    *,
    reviewer: str,
    change_identity: bool = False,
) -> str:
    """A person repairs the drifted bundle on ``tenant`` and approves it.

    Stands in for ``teach`` or ``--save-healed-to`` followed by the governed
    lifecycle: the repaired bundle points at the 7.2 controls, a person
    reviews it, the canary runs on the tenant's own cases, and a person
    approves. ``change_identity`` also changes the identity band, which the
    exported patch must flag.
    """
    proposed = tenant.root / "bundles" / "repaired"
    if proposed.exists():
        shutil.rmtree(proposed)
    band = tenant.member_band
    if change_identity:
        band = tenant.member_band.replace("Member ", "Member: ")
        tenant.member_band = band
    build_bundle(
        proposed,
        version=tenant.portal_version,
        member_band=band,
        member_key=tenant.member_key,
    )
    store = tenant.store
    candidate = store.add_candidate(
        build_candidate(tenant.bundle, proposed, source="manual")
    )
    person = Actor(kind="human", id=reviewer)
    store.review(candidate.candidate_id, person)
    canary_patch(
        store,
        candidate.candidate_id,
        tenant,
        tenant.cases(),
        tenant.root / "canary" / "local",
    )
    store.approve(candidate.candidate_id, person)
    store.stage(candidate.candidate_id, person)
    store.start_canary(
        candidate.candidate_id, person, max_runs=tenant.policy.canary_max_runs
    )
    return candidate.candidate_id


def live_canary(tenant: SimulatedTenant, candidate_id: str) -> str:
    """Record the bounded live canary window with real runs."""
    store = tenant.store
    candidate = store.load_candidate(candidate_id)
    index = 0
    while candidate.state == "canary":
        bundle = store.bundle_path(candidate.proposed_content_digest)
        report = tenant.replay(bundle, tenant.root / "runs" / f"live-{index}")
        candidate = store.record_canary_run(
            candidate_id, observation_from_report(report)
        )
        index += 1
    return candidate.state


def receive(
    fleet: Fleet, tenant: SimulatedTenant, patch: SignedStructuralPatch
) -> PromotionDecision:
    """Import one patch on ``tenant`` and let its policy decide."""
    assert tenant.certificate is not None
    result = import_patch(
        tenant.store,
        patch,
        trust=tenant.trust,
        tenant_bundle=tenant.bundle,
        tenant_app=tenant.app(),
        out_dir=tenant.root / "bundles" / f"patch-{patch.patch_id[:12]}",
    )
    return process_imported_patch(
        tenant.store,
        result.candidate.candidate_id,
        policy=tenant.policy,
        runner=tenant,
        cases=tenant.cases(),
        work_dir=tenant.root / "canary" / patch.patch_id[:12],
    )


@contextmanager
def _private_durable_authority(root: Path) -> Iterator[None]:
    """Keep the simulated runs' durable-run claims inside ``root``.

    Without this, every simulated run would claim its run path in the user's
    machine-wide authority database (``~/.openadapt/durable-authority``).
    """
    from openadapt_flow.runtime.durable.authority import AUTHORITY_DB_ENV

    previous = os.environ.get(AUTHORITY_DB_ENV)
    os.environ[AUTHORITY_DB_ENV] = str(root / "durable-authority.sqlite3")
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(AUTHORITY_DB_ENV, None)
        else:
            os.environ[AUTHORITY_DB_ENV] = previous


def run_scenario(root: Path, *, log: Any = print) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    # The durable-run authority refuses a path through a symlink (macOS
    # /tmp is one), so work on the resolved path.
    root = root.resolve()
    with _private_durable_authority(root):
        return _run_scenario(root, log=log)


def _run_scenario(root: Path, *, log: Any) -> dict[str, Any]:
    fleet = build_fleet(root)
    north, east, west = (fleet.tenants[n] for n in ("north", "east", "west"))
    summary: dict[str, Any] = {}

    healthy = north.replay(north.bundle, north.root / "runs" / "before-upgrade")
    log(f"north on 7.1: {healthy.execution_outcome}, model calls {healthy.model_calls}")

    for tenant in (north, east):
        tenant.portal_version = "7.2"
    halted = north.replay(north.bundle, north.root / "runs" / "after-upgrade")
    log(f"north on 7.2 before repair: {halted.execution_outcome}")
    summary["north_after_upgrade"] = halted.execution_outcome

    candidate_id = local_repair(north, reviewer="north-ops-lead")
    log(
        f"north repair approved by a person; live canary -> {live_canary(north, candidate_id)}"
    )

    assert north.certificate is not None
    exported = export_patch(
        north.store,
        candidate_id,
        workflow_family=WORKFLOW_FAMILY,
        app_before=app_fingerprint("7.1"),
        app_after=app_fingerprint("7.2"),
        signer_key=north.signer_key,
        certificate=north.certificate,
    )
    fleet.feed.publish(exported.patch)
    log(f"north exported patch {exported.patch.patch_id[:16]} to the feed")
    summary["patch_id"] = exported.patch.patch_id

    for patch in fleet.feed.list_patches(app_family=APP_FAMILY):
        decision = receive(fleet, east, patch)
        log(f"east: {decision.outcome} ({'; '.join(decision.reasons)})")
        summary["east"] = decision.outcome
        if decision.outcome == "auto_promoted" and decision.candidate is not None:
            state = live_canary(east, decision.candidate.candidate_id)
            log(f"east live canary -> {state}")
            summary["east_final"] = state

        try:
            receive(fleet, west, patch)
        except PatchImportError as exc:
            log(f"west on 7.1: refused ({exc})")
            summary["west_on_7_1"] = "refused"
        west.portal_version = "7.2"
        decision = receive(fleet, west, patch)
        log(f"west on 7.2: {decision.outcome} ({'; '.join(decision.reasons)})")
        summary["west"] = decision.outcome

    summary["model_calls"] = {t.name: t.model_calls for t in fleet.tenants.values()}
    log(f"model calls across every replay: {summary['model_calls']}")
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--out", required=True, help="Directory for the simulated fleet"
    )
    args = parser.parse_args(argv)
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        parser.error(f"{out} is not empty")
    summary = run_scenario(out)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
