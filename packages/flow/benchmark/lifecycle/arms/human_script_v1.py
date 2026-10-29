"""Register one referral in OpenEMR: the hand-written baseline script (v1).

Written once by the session as a skilled engineer would, against the same
demonstration, procedure and REST read the other arms get. Authoring time is
in ``human_script.py`` (AUTHORING). Runs under the script contract in
``scripts.py``.
"""

import datetime
import re
import time


def _name(v):
    v = re.sub(r"\s+", " ", (v or "").strip())
    return v.title() if v.isupper() or v.islower() else v


def _dob(v):
    v = (v or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(v, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return "BAD"


def _one_char_apart(a, b):
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


def _visible(form, sel):
    return form.locator(sel + " >> visible=true").first


def _confirm_or_matches(page, timeout_s=30.0):
    """('confirm', locator) when OpenEMR found no match, ('matches', text) otherwise."""

    end = time.time() + timeout_s
    while time.time() < end:
        for fr in page.frames:
            try:
                btn = fr.locator("#confirmCreate")
                if btn.count() and btn.first.is_visible():
                    body = fr.locator("body").inner_text()
                    if "No matches were found" in body:
                        ready = btn.first.evaluate(
                            "e => e.ownerDocument.readyState === 'complete' && "
                            "typeof e.ownerDocument.defaultView.dlgclose === 'function'"
                        )
                        if ready:
                            return "confirm", btn.first
                    else:
                        return "matches", body[:300]
            except Exception:
                continue
        page.wait_for_timeout(250)
    return None, None


def _fill(ctx, r):
    form = ctx.form
    form.locator("#form_title").select_option(label=r["title"]) if r.get("title") else None
    for key in ("fname", "lname", "DOB"):
        loc = _visible(form, f"#form_{key}")
        loc.fill(r[key])
        loc.dispatch_event("change")
    form.locator("#form_sex").select_option(label=r["sex"])
    contact = form.get_by_text("Contact", exact=True).first
    contact.scroll_into_view_if_needed()
    contact.click()
    form.wait_for_timeout(300)
    for key in ("street", "city", "postal_code", "phone_home", "email"):
        loc = _visible(form, f"#form_{key}")
        loc.fill(r.get(key, "") or "")
        loc.dispatch_event("change")
    _visible(form, "#form_state").select_option(label=r["state_label"])
    _visible(form, "#form_country_code").select_option(label=r.get("country_code") or "USA")


def register(ctx, referral):
    r = dict(referral)
    r["fname"], r["lname"] = _name(r.get("fname")), _name(r.get("lname"))
    dob = _dob(r.get("DOB"))
    if dob is None:
        return {"result": "NOT SAVED", "reason": "no date of birth; return the referral to the sender"}
    if dob == "BAD":
        return {"result": "NOT SAVED", "reason": f"unreadable date of birth {r.get('DOB')!r}"}
    r["DOB"] = dob

    same_name = [p for p in ctx.find_patients(fname=r["fname"], lname=r["lname"]) if "error" not in p]
    for p in same_name:
        if p.get("DOB") == dob:
            return {"result": "NOT SAVED", "reason": f"patient already has a chart (pid {p.get('pid')})"}
        if _one_char_apart(p.get("DOB") or "", dob) and (p.get("street") or "").upper() == r.get("street", "").upper():
            return {"result": "NOT SAVED", "reason": f"same person as pid {p.get('pid')}; the date of birth looks like a typo"}

    for attempt in (1, 2):
        try:
            _fill(ctx, r)
            ctx.form.locator("#create").click(timeout=15_000)
        except Exception as exc:
            return {"result": "NOT SAVED", "reason": f"UNEXPECTED: form step failed: {str(exc).splitlines()[0][:200]}"}
        kind, what = _confirm_or_matches(ctx.page)
        if kind == "matches":
            return {"result": "NOT SAVED", "reason": "OpenEMR's duplicate check lists a matching patient"}
        if kind is None:
            return {"result": "NOT SAVED", "reason": "UNEXPECTED: no duplicate-check dialog after Create"}
        what.click()
        if not ctx.commit:
            return {"result": "NOT SAVED", "reason": "DRY RUN reached confirm"}
        found = []
        for _ in range(20):
            ctx.page.wait_for_timeout(500)
            found = [p for p in ctx.find_patients(fname=r["fname"], lname=r["lname"], DOB=dob) if "error" not in p]
            if found:
                break
        if len(found) == 1:
            p = found[0]
            if (p.get("street") or "").upper() == r["street"].upper() and (p.get("email") or "") == r["email"]:
                return {"result": "SAVED", "reason": f"read back pid {p.get('pid')}"}
            return {"result": "NOT SAVED", "reason": f"read-back differs for pid {p.get('pid')}"}
        if len(found) > 1:
            return {"result": "NOT SAVED", "reason": f"read-back found {len(found)} charts"}
        ctx.log(f"attempt {attempt}: no chart after confirm")
        if attempt == 1:
            ctx.page.reload()
            ctx.page.wait_for_timeout(1000)
            # The runner's blank form is gone after a reload; reopen it.
            ctx.page.evaluate(
                "() => { window.navigateTab('/interface/new/new_comprehensive.php', "
                "'openadapt-new-patient'); window.activateTabByName('openadapt-new-patient', true); }"
            )
            ctx.page.wait_for_timeout(2000)
            for fr in ctx.page.frames:
                if "new_comprehensive.php" in (fr.url or ""):
                    ctx.form = fr
    return {"result": "NOT SAVED", "reason": "the save did not land after two attempts"}
