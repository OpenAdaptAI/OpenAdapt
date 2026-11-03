import re, time, json

def run(ctx, params):
    page = ctx.page
    def ns(r): return {"result": "NOT SAVED", "reason": r}
    name = params["patient"]; typ = params["type"]; note = params["note"]
    try:
        try:
            page.locator("#notice-continue").click(timeout=1500)
        except Exception:
            pass
        row = page.locator("tr").filter(has_text=name)
        try:
            row.first.wait_for(timeout=3000)
        except Exception:
            return ns("UNEXPECTED: patient not in the task list")
        cands = [r for r in range(row.count()) if row.nth(r).locator("button", has_text="Open").count()]
        matches = [r for r in cands if row.nth(r).locator("td").first.inner_text().strip() == name]
        if len(matches) != 1:
            return ns("UNEXPECTED: patient row not found uniquely")
        row.nth(matches[0]).locator("button", has_text="Open").click(timeout=5000)
        page.locator("#new-encounter").wait_for(timeout=5000)
        txt = page.locator("body").inner_text(timeout=5000)
        m = re.search(r"(.+?)\s+\S\s+MRN\s+(\S+)\s+\S\s+DOB", txt)
        if not m or m.group(1).strip().split("\n")[-1].strip() != name:
            return ns("UNEXPECTED: banner does not show " + name)
        mrn = m.group(2).lower()
        before = ctx.records()
        for r in before:
            if r["patient_id"] == mrn and r["type"] == typ and r["note"] == note:
                return ns("DUPLICATE: matching record already exists")
        page.click("#new-encounter", timeout=5000)
        page.locator("#save-encounter").wait_for(timeout=5000)
        txt = page.locator("body").inner_text(timeout=5000)
        if name not in txt or not re.search(r"MRN\s+" + re.escape(m.group(2)), txt):
            return ns("UNEXPECTED: banner changed on encounter screen")
        page.click("#type-" + typ.lower(), timeout=5000)
        page.fill("#note", note, timeout=5000)
        page.click("#save-encounter", timeout=5000)
    except Exception as e:
        return ns("UNEXPECTED: " + str(e)[:150])
    if not ctx.commit:
        return ns("DRY RUN reached save")
    try:
        page.locator("#saved-banner, #save-error").first.wait_for(timeout=5000)
    except Exception:
        pass
    after = ctx.records()
    old = {r["id"]: r for r in before}
    new = [r for r in after if r["id"] not in old]
    for i, r in old.items():
        if not any(a["id"] == i and a == r for a in after):
            return ns("READBACK: earlier row removed or changed")
    if len(new) != 1 or len(after) != len(before) + 1:
        return ns("READBACK: expected exactly one new row, got %d" % len(new))
    n = new[0]
    if n["patient_id"] == mrn and n["type"] == typ and n["note"] == note:
        return {"result": "SAVED", "reason": "Exactly one new matching record found."}
    return ns("READBACK: new row does not match")
