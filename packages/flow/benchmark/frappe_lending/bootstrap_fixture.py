"""Executed inside ``bench --site frontend console``; synthetic data only.

This file intentionally uses the pinned applications' own test setup helpers.
It must never be executed against a production or customer site.
"""

from importlib import import_module

import frappe
from frappe.desk.page.setup_wizard.setup_wizard import enable_setup_wizard_complete
from frappe.permissions import add_permission, get_valid_perms, reset_perms
from frappe.utils import now_datetime
from frappe.utils.password import check_password, update_password

ACTOR = "openadapt.actor@example.invalid"
ACTOR_PASSWORD = "openadapt-local-actor"
ORACLE = "openadapt.oracle@example.invalid"
ORACLE_PASSWORD = "openadapt-local-oracle"
APPLICANT = "OpenAdapt Synthetic Applicant"
PRODUCT = "OpenAdapt Synthetic Term Loan"

# The pinned v16 readiness predicate is ``frappe.is_setup_complete()``, which
# requires both framework and ERPNext Installed Application rows to be marked
# complete. A new non-interactive site otherwise redirects every desk route to
# /desk/setup-wizard/0 even though the apps and test masters exist.
for app_name in ("frappe", "erpnext", "lending"):
    if frappe.db.exists("Installed Application", {"app_name": app_name}):
        enable_setup_wizard_complete(app_name)
if not frappe.is_setup_complete():
    raise RuntimeError("pinned Frappe setup-complete predicate is false")

# Importing ERPNext/Lending test helpers triggers their module-level synthetic
# test-data bootstrap. Resolve them only after setup, and make the intended test
# context explicit so their known fixture passwords are not rejected by live
# password-strength policy. Restore the flag before creating benchmark users.
# This fixture is isolated and synthetic; it must never run on customer data.
previous_in_test = frappe.in_test
frappe.in_test = True
try:
    lending_test_utils = import_module("lending.tests.test_utils")
    before_tests = lending_test_utils.before_tests
    create_loan_product = lending_test_utils.create_loan_product
    before_tests()
finally:
    frappe.in_test = previous_in_test


def ensure_system_user(email, first_name, role, password):
    if not frappe.db.exists("User", email):
        frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": first_name,
                "enabled": 1,
                "user_type": "System User",
                "send_welcome_email": 0,
                "roles": [{"doctype": "Has Role", "role": role}],
            }
        ).insert(ignore_permissions=True)
    else:
        user = frappe.get_doc("User", email)
        user.enabled = 1
        user.user_type = "System User"
        if not any(item.role == role for item in user.roles):
            user.append("roles", {"role": role})
        user.save(ignore_permissions=True)
    update_password(email, password)


ensure_system_user(ACTOR, "OpenAdapt Actor", "Loan Manager", ACTOR_PASSWORD)

if not frappe.db.exists("Customer", APPLICANT):
    customer_group = frappe.get_all(
        "Customer Group", filters={"is_group": 0}, pluck="name", limit=1
    )[0]
    territory = frappe.get_all(
        "Territory", filters={"is_group": 0}, pluck="name", limit=1
    )[0]
    frappe.get_doc(
        {
            "doctype": "Customer",
            "customer_name": APPLICANT,
            "customer_type": "Individual",
            "customer_group": customer_group,
            "territory": territory,
        }
    ).insert(ignore_permissions=True)

create_loan_product(
    PRODUCT,
    PRODUCT,
    500000,
    9.2,
    0,
    1,
    0,
    repayment_schedule_type="Monthly as per repayment start date",
)

# Use the NANP country context so the benchmark can use the officially
# reserved 202-555-0100 fictional number instead of a potentially assigned
# local number. Persist through the supported Single DocType; its on_update
# synchronizes the boot-time global default used by the phone widget.
global_defaults = frappe.get_single("Global Defaults")
global_defaults.country = "United States"
global_defaults.save(ignore_permissions=True)
if frappe.db.get_default("country") != "United States":
    raise RuntimeError("synthetic fixture country default is not United States")

if not frappe.db.exists("Role", "OpenAdapt Read-only Oracle"):
    frappe.get_doc(
        {"doctype": "Role", "role_name": "OpenAdapt Read-only Oracle"}
    ).insert(ignore_permissions=True)

# In Frappe, the presence of *any* Custom DocPerm for a DocType replaces every
# standard DocPerm for that DocType in ``get_valid_perms``. Inserting only the
# oracle row would therefore remove Loan Manager's UI/API access. Rebuild this
# synthetic fixture's Loan Application custom matrix through the supported
# helpers: ``add_permission`` first copies all pinned upstream rules, then adds
# the one read-only oracle rule. This site contains no customer customizations.
reset_perms("Loan Application")
add_permission(
    "Loan Application",
    "OpenAdapt Read-only Oracle",
    permlevel=0,
    ptype="read",
)
# Loan Application requires a Company link, but upstream's Loan Manager role
# has no Company access and Frappe raises a blocking permission dialog before
# the task begins. Add only Company read to that same actor role through the
# supported copied permission matrix. No Customer access or write privilege is
# added.
reset_perms("Company")
add_permission("Company", "Loan Manager", permlevel=0, ptype="read")
# The pinned form setup reads ``employee_loans`` from this Single DocType to
# decide whether Applicant Type is visible. Grant only the read it performs.
reset_perms("Loan Origination Settings")
add_permission("Loan Origination Settings", "Loan Manager", permlevel=0, ptype="read")
# Loan Application's pinned form script queries Loan Purpose metadata even
# when the optional field is blank. Keep that lookup read-only; the benchmark
# actor must not create or modify purpose masters.
reset_perms("Loan Purpose")
add_permission("Loan Purpose", "Loan Manager", permlevel=0, ptype="read")

ensure_system_user(
    ORACLE,
    "OpenAdapt Oracle",
    "OpenAdapt Read-only Oracle",
    ORACLE_PASSWORD,
)

# The baseline contains no target effects. Every trial restores this state.
frappe.db.delete("Loan Application", {"applicant": APPLICANT})
# Loan Application's pinned autoname is ``ACC-LOAP-.YYYY.-.#####``. Seed its
# current-year series row without consuming a business document so each trial
# changes the series value but not its row count; the exact cardinality
# contract can consequently require every non-target table to remain at +0.
series_key = f"ACC-LOAP-{now_datetime().year}-"
frappe.db.sql(
    "INSERT IGNORE INTO `tabSeries` (`name`, `current`) VALUES (%s, 0)",
    (series_key,),
)
frappe.clear_cache()
frappe.db.commit()  # nosemgrep: local synthetic fixture setup

# Refuse the baseline unless the exact credentials and role boundary work.
check_password(ACTOR, ACTOR_PASSWORD, delete_tracker_cache=False)
check_password(ORACLE, ORACLE_PASSWORD, delete_tracker_cache=False)
if frappe.db.get_default("country") != "United States":
    raise RuntimeError("verified fixture country default changed unexpectedly")
actor_perms = get_valid_perms("Loan Application", user=ACTOR)
oracle_perms = get_valid_perms("Loan Application", user=ORACLE)
actor_company_perms = get_valid_perms("Company", user=ACTOR)
actor_origination_settings_perms = get_valid_perms(
    "Loan Origination Settings", user=ACTOR
)
actor_loan_purpose_perms = get_valid_perms("Loan Purpose", user=ACTOR)
if not any(permission.create for permission in actor_perms):
    raise RuntimeError("actor valid-permission matrix cannot create Loan Application")
if not any(permission.read for permission in actor_perms):
    raise RuntimeError("actor valid-permission matrix cannot read Loan Application")
if not frappe.has_permission("Loan Application", "create", user=ACTOR):
    raise RuntimeError("actor cannot create Loan Application")
if not any(permission.read for permission in actor_company_perms):
    raise RuntimeError("actor valid-permission matrix cannot read Company")
if not frappe.has_permission("Company", "read", user=ACTOR):
    raise RuntimeError("actor cannot read Company")
for forbidden in ("create", "write", "delete", "submit", "cancel", "amend"):
    if any(permission.get(forbidden) for permission in actor_company_perms):
        raise RuntimeError(
            f"actor Company matrix unexpectedly has {forbidden} permission"
        )
    if frappe.has_permission("Company", forbidden, user=ACTOR):
        raise RuntimeError(f"actor unexpectedly has Company {forbidden} permission")
if not any(permission.read for permission in actor_origination_settings_perms):
    raise RuntimeError("actor cannot read Loan Origination Settings")
if not frappe.has_permission("Loan Origination Settings", "read", user=ACTOR):
    raise RuntimeError("actor cannot read Loan Origination Settings")
for forbidden in ("create", "write", "delete", "submit", "cancel", "amend"):
    if any(
        permission.get(forbidden) for permission in actor_origination_settings_perms
    ):
        raise RuntimeError(
            "actor Loan Origination Settings matrix unexpectedly has " + forbidden
        )
    if frappe.has_permission("Loan Origination Settings", forbidden, user=ACTOR):
        raise RuntimeError(
            "actor unexpectedly has Loan Origination Settings " + forbidden
        )
if not any(permission.read for permission in actor_loan_purpose_perms):
    raise RuntimeError("actor cannot read Loan Purpose")
if not frappe.has_permission("Loan Purpose", "read", user=ACTOR):
    raise RuntimeError("actor cannot read Loan Purpose")
for forbidden in ("create", "write", "delete", "submit", "cancel", "amend"):
    if any(permission.get(forbidden) for permission in actor_loan_purpose_perms):
        raise RuntimeError(
            f"actor Loan Purpose matrix unexpectedly has {forbidden} permission"
        )
    if frappe.has_permission("Loan Purpose", forbidden, user=ACTOR):
        raise RuntimeError(
            f"actor unexpectedly has Loan Purpose {forbidden} permission"
        )
if not any(permission.read for permission in oracle_perms):
    raise RuntimeError("oracle valid-permission matrix cannot read Loan Application")
if not frappe.has_permission("Loan Application", "read", user=ORACLE):
    raise RuntimeError("oracle cannot read Loan Application")
for forbidden in ("create", "write", "delete", "submit", "cancel", "amend"):
    if any(permission.get(forbidden) for permission in oracle_perms):
        raise RuntimeError(
            f"oracle valid-permission matrix unexpectedly has {forbidden} permission"
        )
    if frappe.has_permission("Loan Application", forbidden, user=ORACLE):
        raise RuntimeError(f"oracle unexpectedly has {forbidden} permission")
if frappe.db.exists("Loan Application", {"applicant": APPLICANT}):
    raise RuntimeError("baseline still contains a target Loan Application")
# Construct the sentinel so a traceback echoing this source cannot contain the
# completed marker. The host requires this exact line, never a substring.
print("OPENADAPT_FRAPPE_" + "FIXTURE_READY")
