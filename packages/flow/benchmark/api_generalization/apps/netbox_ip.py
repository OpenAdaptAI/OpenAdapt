"""NetBox: an engineer records a new IP address (IPAM > IP Addresses > Add)."""

from __future__ import annotations

import random
from typing import Any

from benchmark.api_generalization.apps.nb import NB_C, NetBoxBase, nbsql
from benchmark.api_generalization.common import (
    Case,
    ExecSqlVerifier,
    podman,
    rec_click,
    rec_text,
)
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr

QUERY = ("SELECT id, host(address) || '/' || masklen(address) AS address, dns_name, description "
         "FROM ipam_ipaddress")
JUDGE = ("SELECT id, host(address) || '/' || masklen(address) AS address, status, dns_name, description, "
         "COALESCE(custom_field_data->>'owner_team', '') AS owner_team, role, tenant_id, vrf_id, comments "
         "FROM ipam_ipaddress ORDER BY id")


def set_cf_default(value: str) -> None:
    podman("exec", "-i", NB_C, "/opt/netbox/venv/bin/python", "/opt/netbox/netbox/manage.py", "shell",
           input=f"from extras.models import CustomField\ncf=CustomField.objects.get(name='owner_team'); "
                 f"cf.default={value!r}; cf.save()\n", timeout=300)


class App(NetBoxBase):
    name = "netbox_ip"
    workflow = "add-ip-address"
    form_path = "/ipam/ip-addresses/add/"
    save_path_regex = r"^/ipam/ip-addresses/add/$"
    unique_param = "address"
    normal_values = [
        dict(address="10.77.0.10/24", dns_name="printer-2f.example.invalid", description="Second floor printer"),
        dict(address="10.77.0.11/24", dns_name="door-ctl.example.invalid", description="Badge door controller"),
        dict(address="10.77.0.12/24", dns_name="kiosk-lobby.example.invalid", description="Lobby kiosk"),
    ]
    blank_values = dict(address="10.77.0.13/24", dns_name="", description="")
    dup_values = dict(address="10.77.0.14/24", dns_name="same.example.invalid", description="same.example.invalid")
    invalid_values = dict(address="10.77.0.300/24", dns_name="bad-octet.example.invalid", description="Octet over 255")
    invalid_note = "an address with an octet over 255 (the server rejects it)"
    default_column = "owner_team"
    changed_default_value = "Platform"

    def unique(self, case: Case, tag: str) -> Case:
        if case.kind == "validation":
            return case
        taken = {r["address"] for r in nbsql(QUERY)}
        rng = random.Random(tag)
        while True:
            addr = f"10.77.{rng.randrange(1, 250)}.{rng.randrange(1, 250)}/24"
            if addr not in taken:
                break
        params = dict(case.params, address=addr)
        fields = dict(case.expect_fields)
        if fields:
            fields["address"] = addr
        return Case(case.id, case.kind, params, case.expect_new, fields, case.fault, case.note)

    def record(self, rec: Any, page: Any, v: dict[str, str]) -> None:
        rec_text(rec, page, "#id_address", v["address"], "address")
        rec_text(rec, page, "#id_dns_name", v["dns_name"], "dns_name")
        rec_text(rec, page, "#id_description", v["description"], "description")
        rec_click(rec, page, "button[name=_create]")

    def change_default(self) -> None:
        set_cf_default("Platform")

    def restore_default(self) -> None:
        set_cf_default("Network Ops")

    def rows(self) -> list[dict[str, Any]]:
        return nbsql(JUDGE)

    def verifier(self) -> Any:
        return ExecSqlVerifier(nbsql, QUERY, timeout_s=10.0)

    def expect_fields(self, v: dict[str, str]) -> dict[str, str]:
        return {"address": v["address"], "dns_name": v["dns_name"], "description": v["description"],
                "status": "active", "owner_team": "Network Ops"}

    def effects(self) -> list[Effect]:
        match = {"address": ValueExpr(param="address")}
        out = [Effect(kind=EffectKind.RECORD_WRITTEN, match=match, expected_count=1, risk="reversible",
                      probe="exactly one IP address record with this address", timeout_s=8.0)]
        for f in ("dns_name", "description"):
            out.append(Effect(kind=EffectKind.FIELD_EQUALS, match=match, field=f, value=ValueExpr(param=f),
                              risk="reversible", probe=f"IP address {f} read-back", timeout_s=8.0))
        return out
