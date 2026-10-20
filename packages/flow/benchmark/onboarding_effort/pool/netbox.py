"""NetBox 4.1 workflows (Apache-2.0 network inventory, synthetic data).

The engineer's verify.yaml reads PostgreSQL as the read-only role
``nb_reader`` (``kind: sql``, driver ``pg8000``). The oracle reads through
``podman exec psql`` as the owner.
"""

from __future__ import annotations

import os
import random
from typing import Any

from benchmark.onboarding_effort.apps import netbox_app as N
from benchmark.onboarding_effort.pool.base import FIRST, Verdict, Workflow, center, click, fill

# The engineer stages the reader's password for the verifier (throwaway, local).
os.environ.setdefault("NB_READER_PASSWORD", N.READER_PASS)

SITES = ("Lisbon Lab", "Oslo Edge", "Quito Branch")
ROLES = ("Core Switch", "Access Point", "Edge Router")
TYPES = ("SX-24", "SX-48", "AP-7", "ER-2")
GROUPS = ("Campus", "Datacenter")


def tomselect(page: Any, rec: Any, field: str, label: str, param: str | None) -> None:
    """A NetBox dynamic dropdown: open it, type to search, pick with Enter."""
    rec.click(*center(page, rec, f"#id_{field} ~ .ts-wrapper .ts-control"))
    rec.type_text(label, param=param)
    page.wait_for_selector(f".ts-dropdown .option:has-text('{label}')", timeout=30_000)
    page.wait_for_timeout(300)
    rec.press("Enter")


class NetBoxBase(Workflow):
    app = "netbox"
    policy = "permissive"
    seed = 11

    def user(self, installation: str) -> str:
        return "admin"

    def reset(self, installation: str) -> None:
        N.reset()
        self.apply_installation(installation)

    def start(self, page: Any, installation: str) -> None:
        N.login(page, self.user(installation))
        page.goto(self.entry_url(installation))
        page.wait_for_load_state("load")

    def _verifier(self, query: str) -> str:
        return f"""verifier:
  kind: sql
  sql_driver: pg8000.dbapi
  sql_connect_args: {{host: 127.0.0.1, port: {N.PG_PORT}, user: nb_reader, database: netbox}}
  sql_password_env: NB_READER_PASSWORD
  sql_query: "{query}"
"""

    def _yaml(self, installation: str, verifier: str, record: str) -> str:
        return f"""schema_version: openadapt.verify/v1
app:
  name: NetBox (local test copy)
  url: {self.entry_url(installation)}
  version: netbox@4.1.11
  session: benchmark.onboarding_effort.hooks:open_session
  reset:
    python: benchmark.onboarding_effort.hooks:reset
{verifier}{record}save_step: auto
inputs:
  source: recorded
campaign:
  id: onboarding-{self.id}-{installation}
  trials: 3
  policy: {self.policy}
"""


class NetBoxDeviceAdd(NetBoxBase):
    id = "nb-device-add"
    shape = "single"
    inputs = ("name", "site", "role", "device_type", "serial")
    intent = "Add a device with its name, site, role, device type and serial number."
    second_installation = "the user's dark color mode (user preference ui.colormode = dark)"
    second_kind = "theme"

    def apply_installation(self, installation: str) -> None:
        mode = "light" if installation == FIRST else "dark"
        N.shell(
            "from users.models import User\nu=User.objects.get(username='admin')\n"
            f"u.config.set('ui.colormode', '{mode}', commit=True)\nprint('ok')\n"
        )

    def entry_url(self, installation: str) -> str:
        return N.BASE + "/dcim/devices/add/"

    def make_item(self, rng: random.Random, n: int, split: str) -> dict[str, str]:
        return {
            "name": f"onb-{split}-{n:02d}-{rng.randrange(10**5):05d}",
            "site": rng.choice(SITES),
            "role": rng.choice(ROLES),
            "device_type": rng.choice(TYPES),
            "serial": f"SYN{rng.randrange(10**8):08d}",
        }

    def demonstrate(self, rec: Any, page: Any, item: dict[str, str]) -> None:
        fill(page, rec, "#id_name", item["name"], "name")
        tomselect(page, rec, "role", item["role"], "role")
        tomselect(page, rec, "device_type", item["device_type"], "device_type")
        tomselect(page, rec, "site", item["site"], "site")
        fill(page, rec, "#id_serial", item["serial"], "serial")
        click(page, rec, "button[name=_create]")
        page.wait_for_url("**/dcim/devices/*/")

    def verify_yaml(self, installation: str) -> str:
        q = ("SELECT d.name, s.name AS site, r.name AS role, t.model AS device_type, d.serial "
             "FROM dcim_device d JOIN dcim_site s ON s.id=d.site_id JOIN dcim_devicerole r "
             "ON r.id=d.role_id JOIN dcim_devicetype t ON t.id=d.device_type_id")
        record = """record:
  name: device
  match: [name]
  duplicate_key: name
  fields:
    name: name
    site: site
    role: role
    device_type: device_type
    serial: serial
"""
        return self._yaml(installation, self._verifier(q), record)

    def snapshot(self) -> Any:
        rows = N.nbsql(
            "SELECT d.id, d.name, s.name AS site, r.name AS role, t.model AS device_type, d.serial "
            "FROM dcim_device d JOIN dcim_site s ON s.id=d.site_id JOIN dcim_devicerole r "
            "ON r.id=d.role_id JOIN dcim_devicetype t ON t.id=d.device_type_id")
        return {r["id"]: r for r in rows}

    def judge(self, item: dict[str, str], before: Any, after: Any) -> Verdict:
        new = [r for k, r in after.items() if k not in before]
        mine = [r for r in new if r["name"] == item["name"]]
        changed = [k for k, r in before.items() if after.get(k) != r]
        if not mine:
            return Verdict("MISSING", {"new": len(new)})
        want = {k: item[k] for k in ("site", "role", "device_type", "serial")}
        got = {k: mine[0][k] for k in want}
        ok = len(new) == 1 and got == want and not changed
        return Verdict("VERIFIED" if ok else "WRONG", {"want": want, "got": got, "changed": changed})


class NetBoxIpPaginated(NetBoxBase):
    id = "nb-ip-paginated"
    shape = "paginated"
    batch_run = True
    inputs = ("description",)
    keys = ("address",)
    intent = (
        "Go through every IP address tagged onb-audit, ten to a page: open each one, set "
        "its description to the audit note, save, and return to the list; then the next page."
    )
    second_installation = "screen 1600x1000 at device scale 2"
    second_kind = "screen"

    def installation_viewport(self, installation: str) -> tuple[int, int]:
        return self.viewport if installation == FIRST else (1600, 1000)

    def device_scale(self, installation: str) -> float:
        return 1.0 if installation == FIRST else 2.0

    def entry_url(self, installation: str) -> str:
        return N.BASE + "/ipam/ip-addresses/?tag=onb-audit&per_page=10&ordering=address"

    def make_item(self, rng: random.Random, n: int, split: str) -> dict[str, str]:
        third = 1 + (abs(hash(split)) % 200)
        return {"address": f"198.51.{third}.{10 + n}/24",
                "description": "Audited 2026-10" if not split.startswith("demo") else f"Audited demo {n}"}

    def batch_params(self, items: list[dict[str, str]]) -> dict[str, str]:
        return {"description": items[0]["description"]}

    def prepare_items(self, items: list[dict[str, str]], installation: str) -> None:
        addrs = [i["address"] for i in items]
        N.shell(
            "from ipam.models import IPAddress\nfrom extras.models import Tag\n"
            "t=Tag.objects.get(slug='onb-audit')\n"
            f"for a in {addrs!r}:\n"
            "    ip,_=IPAddress.objects.get_or_create(address=a)\n    ip.tags.add(t)\n"
            "IPAddress.objects.get_or_create(address='203.0.113.9/24', defaults={'description':'decoy, untagged'})\n"
            "print('ok')\n"
        )

    def demonstrate(self, rec: Any, page: Any, item: dict[str, str]) -> None:
        # One iteration: open the first address still without a description.
        row = page.locator("table tbody tr", has_not_text="Audited").first
        click(page, rec, row.locator("td a").first)
        page.wait_for_url("**/ipam/ip-addresses/*/")
        click(page, rec, "a:has-text('Edit')")
        page.wait_for_selector("#id_description")
        fill(page, rec, "#id_description", item["description"], "description")
        click(page, rec, "button[name=_update], button:has-text('Save')")
        page.wait_for_url("**/ipam/ip-addresses/*/")
        rec.press("Alt+ArrowLeft")
        rec.press("Alt+ArrowLeft")
        page.wait_for_selector("table tbody tr")

    def demonstrate_keyed(self, rec: Any, page: Any, item: dict[str, str]) -> None:
        fill(page, rec, "input[name=q]", item["address"].split("/")[0], "address")
        rec.press("Enter")
        page.wait_for_selector("table tbody tr")
        click(page, rec, page.locator("table tbody tr td a", has_text=item["address"].split("/")[0]).first)
        click(page, rec, "a:has-text('Edit')")
        fill(page, rec, "#id_description", item["description"], "description")
        click(page, rec, "button[name=_update], button:has-text('Save')")
        page.wait_for_url("**/ipam/ip-addresses/*/")

    def verify_yaml(self, installation: str) -> str:
        q = "SELECT host(address) || '/' || masklen(address) AS address, description FROM ipam_ipaddress"
        effects = """effects:
  - kind: field_equals
    match: {address: {param: address}}
    field: description
    value: {param: description}
"""
        return self._yaml(installation, self._verifier(q), effects)

    def snapshot(self) -> Any:
        rows = N.nbsql("SELECT id, host(address) || '/' || masklen(address) AS address, description "
                       "FROM ipam_ipaddress")
        return {r["address"]: r for r in rows}

    def judge(self, item: dict[str, str], before: Any, after: Any) -> Verdict:
        b, a = before.get(item["address"]), after.get(item["address"])
        if a is None:
            return Verdict("WRONG", {"reason": "address gone"})
        if a == b:
            return Verdict("MISSING", {})
        return Verdict("VERIFIED" if a["description"] == item["description"] else "WRONG",
                       {"description": a["description"]})

    def judge_batch_collateral(self, before: Any, after: Any, item_ids: set[int]) -> list[str]:
        return [k for k, r in before.items() if after.get(k) != r and not r["address"].startswith("198.51.")]


class NetBoxVlanConditional(NetBoxBase):
    id = "nb-vlan-conditional"
    shape = "conditional"
    inputs = ("vid", "name", "group")
    intent = "Add a VLAN with its ID and name; when the request names a VLAN group, assign that group."
    second_installation = "a required custom field 'Cost center' on VLANs"
    second_kind = "required_field"

    def apply_installation(self, installation: str) -> None:
        if installation == FIRST:
            N.shell("from extras.models import CustomField\n"
                    "CustomField.objects.filter(name='cost_center').delete()\nprint('ok')\n")
        else:
            N.shell(
                "from extras.models import CustomField\nfrom core.models import ObjectType\n"
                "from ipam.models import VLAN\n"
                "cf,_=CustomField.objects.get_or_create(name='cost_center', defaults=dict("
                "type='text', label='Cost center', required=True))\n"
                "cf.object_types.set([ObjectType.objects.get_for_model(VLAN)])\nprint('ok')\n"
            )

    def entry_url(self, installation: str) -> str:
        return N.BASE + "/ipam/vlans/add/"

    def make_item(self, rng: random.Random, n: int, split: str) -> dict[str, str]:
        base = {"demo-first": 100, "heldout-first": 300, "qual-first": 500,
                "demo-second": 700, "heldout-second": 900, "qual-second": 1100}.get(split, 1300)
        return {"vid": str(base + n), "name": f"onb-{split}-{n:02d}",
                "group": rng.choice(("", "", "Campus", "Datacenter"))}

    def branch_key(self, item: dict[str, str]) -> str:
        return "group" if item["group"] else "none"

    def demonstrate(self, rec: Any, page: Any, item: dict[str, str]) -> None:
        fill(page, rec, "#id_vid", item["vid"], "vid")
        fill(page, rec, "#id_name", item["name"], "name")
        if item["group"]:
            tomselect(page, rec, "group", item["group"], "group")
        click(page, rec, "button[name=_create]")
        page.wait_for_url("**/ipam/vlans/*/")

    def verify_yaml(self, installation: str) -> str:
        q = ("SELECT v.vid::text AS vid, v.name, COALESCE(g.name, '') AS group FROM ipam_vlan v "
             "LEFT JOIN ipam_vlangroup g ON g.id=v.group_id")
        record = """record:
  name: vlan
  match: [name]
  duplicate_key: name
  fields:
    vid: vid
    name: name
    group: group
"""
        return self._yaml(installation, self._verifier(q), record)

    def snapshot(self) -> Any:
        rows = N.nbsql("SELECT v.id, v.vid::text AS vid, v.name, COALESCE(g.name, '') AS grp "
                       "FROM ipam_vlan v LEFT JOIN ipam_vlangroup g ON g.id=v.group_id")
        return {r["id"]: r for r in rows}

    def judge(self, item: dict[str, str], before: Any, after: Any) -> Verdict:
        new = [r for k, r in after.items() if k not in before]
        mine = [r for r in new if r["name"] == item["name"]]
        if not mine:
            return Verdict("MISSING", {"new": len(new)})
        r = mine[0]
        ok = len(new) == 1 and r["vid"] == item["vid"] and r["grp"] == item["group"]
        return Verdict("VERIFIED" if ok else "WRONG", {"got": r, "want_group": item["group"]})
