# Odoo 18 Community: create a contact

The skills registry's first non-healthcare candidate. Odoo Community is
LGPL-3.0. Its images are pulled by digest at run time
(`environment.lock.json`), and nothing of Odoo is stored here. All data is
synthetic.

| File | What it is |
| --- | --- |
| `compose.yml`, `fixture.py` | The test copy: Odoo 18.0 and Postgres 16 on loopback ports 9412 and 9413, the `oa` database with Contacts and no demo data. A dedicated user (`openadapt.actor@example.invalid`, with Contact Creation) and a read-only role for the record check (`oa_record_check`). |
| `qualify_hooks.py` | Signs the dedicated user in, opens a new contact, archives contacts between cases, and writes a signed-in browser state (what a registry user binds as `SIGNED_IN_STATE`). |
| `demo.py` | The scripted demonstration (choose Individual, fill name, street, city, ZIP, phone and email, save), then compile. |
| `verify.yaml` | The record check: one parameterized, read-only SELECT of the typed fields from `res_partner`, keyed by email. |
| `results/2026-10-28-click-teach/qualification/` | The qualification evidence. The compiled bundle isn't committed; `demo.py compile` rebuilds it. |

```bash
cd packages/flow
export ODOO_RECORD_PASSWORD=...   # from the fixture's secrets.json
PYTHONPATH=.:benchmark/odoo_contacts python benchmark/odoo_contacts/demo.py record --out REC
PYTHONPATH=.:benchmark/odoo_contacts python benchmark/odoo_contacts/demo.py compile --recording REC --bundle B
PYTHONPATH=.:benchmark/odoo_contacts openadapt-flow qualify B \
  --verify benchmark/odoo_contacts/verify.yaml --out Q --certify clinical-write
```

`PYTHONPATH` includes this folder because an installed `benchmark` package
can shadow this repository's namespace package.

## Result (2026-10-28)

The demonstration saved the contact, and the record check's SELECT read all
six fields back. The compiled program has 14 steps and 6 parameters.
Qualification under `clinical-write` refused, as it should, before any case
ran:

- **Short labels.** The clicks into City and Phone are unarmed: the only
  text beside each is a one-word label, too little for an identity check.
  Reaching them by keyboard from a checked click (Street, Email) works.
  Those keystrokes need the focus check (#1579) to carry a postcondition.
- **An icon-only save button.** Odoo's save button is a cloud icon with no
  text, so its click can't carry an identity check. This is the same class
  as Medplum's Create button (#1581, form identity). Odoo's own save
  shortcut is Control+S on macOS and Alt+S elsewhere, so a taught shortcut
  would replay on one platform only.

So this skill isn't in the registry yet. Under the threat model (#1598) the
record check is what proves the save, and identity checks are fail-closed
pre-checks only, so the next step is the icon-button identity in #1581, not
a page heuristic here.

## Setting the fixture up

- Odoo keeps a long-polling connection open, so a page never goes network
  idle. The hooks wait for the form instead.
- A plain internal user can't create contacts, and Odoo then shows no save
  button. The dedicated user needs Contact Creation
  (`base.group_partner_manager`).

Effort: about 0.8 hours of lease time, from bring-up to the refused
qualification.
