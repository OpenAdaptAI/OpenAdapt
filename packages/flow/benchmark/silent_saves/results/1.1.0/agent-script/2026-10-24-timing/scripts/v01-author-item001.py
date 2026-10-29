import re, time, json

def run(ctx, params):
    p = ctx.page
    name, typ, note = params["patient"], params["type"], params["note"]
    def no(r): return {"result": "NOT SAVED", "reason": r}
    try:
        row = p.locator("#tasks tr, table tr").filter(has_text=name)
        if row.count() != 1:
            return no("UNEXPECTED: patient row not found uniquely")
        row.first.locator("button.open-btn, button").first.click(timeout=5000)
        p.wait_for_selector("#new-encounter", timeout=5000)
        body = p.inner_text("body")
        m = re.search(r"(.+?)\s+\S\s+MRN\s+(\S+)\s+\S\s+DOB", body)
        if not m or m.group(1).strip().split("\n")[-1].strip() != name:
            return no("UNEXPECTED: record banner does not show " + name)
        mrn = m.group(2).lower()
        def dup(rs):
            return [r for r in rs if r["patient_id"] == mrn and r["type"] == typ and r["note"] == note]
        before = ctx.records()
        if dup(before):
            return no("DUPLICATE: matching encounter already exists")
        p.click("#new-encounter", timeout=5000)
        p.click("#type-" + typ.lower(), timeout=5000)
        p.fill("#note", note, timeout=5000)
        if not ctx.commit:
            p.click("#save-encounter", timeout=5000)
            return no("DRY RUN reached save")
        p.click("#save-encounter", timeout=5000)
        try:
            p.wait_for_selector("#saved-banner, #save-error", timeout=5000)
        except Exception:
            pass
        after = ctx.records()
        bk = {r["id"]: r for r in before}
        ak = {r["id"]: r for r in after}
        for i, r in bk.items():
            if i not in ak or ak[i] != r:
                return no("READBACK: earlier row removed or changed")
        new = [r for i, r in ak.items() if i not in bk]
        if len(new) != 1:
            return no("READBACK: expected 1 new row, found %d" % len(new))
        n = new[0]
        if n["patient_id"] != mrn or n["type"] != typ or n["note"] != note:
            return no("READBACK: new row does not match")
        return {"result": "SAVED", "reason": "Encounter saved and verified"}
    except Exception as e:
        return no("UNEXPECTED: " + str(e)[:100])
