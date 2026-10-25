# authored by the agent at setup
import re, time, datetime, json

def _name(s):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    def cap(w):
        if w.isupper() or w.islower():
            return re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(), w)
        return w
    return " ".join(cap(w) for w in s.split(" "))

def _date(s):
    s = (s or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
        if not m:
            return "BAD"
        a, b, y = map(int, m.groups())
        if a > 12 and b <= 12:
            d, mo = a, b
        elif b > 12 and a <= 12:
            mo, d = a, b
        else:
            return "BAD"
    try:
        return datetime.date(y, mo, d).isoformat()
    except ValueError:
        return "BAD"

def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def _typo_dob(a, b):
    if not a or not b or a == b or len(a) != len(b):
        return False
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    if len(diff) == 1:
        return True
    if len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]:
        return True
    return False

def _ns(msg):
    return {"result": "NOT SAVED", "reason": msg}

def register(ctx, referral):
    try:
        return _register(ctx, referral)
    except Exception as e:
        return _ns("UNEXPECTED: " + str(e)[:200])

def _register(ctx, r):
    page = ctx.page
    form = ctx.form
    fname = _name(r.get("fname"))
    lname = _name(r.get("lname"))
    dob = _date(r.get("DOB"))
    if dob is None:
        return _ns("Referral has no date of birth; it goes back to the sender.")
    if dob == "BAD":
        return _ns("UNEXPECTED: date of birth '%s' is malformed or ambiguous." % r.get("DOB"))
    if not fname or not lname:
        return _ns("UNEXPECTED: referral lacks a first or last name.")
    street = (r.get("street") or "").strip()
    city = (r.get("city") or "").strip()
    postal = (r.get("postal_code") or "").strip()
    phone = (r.get("phone_home") or "").strip()
    email = (r.get("email") or "").strip()
    title = (r.get("title") or "").strip()
    sex = (r.get("sex") or "").strip()
    state_label = (r.get("state_label") or "").strip()
    country = (r.get("country_code") or "").strip()

    seen = {}
    for kw in ({"lname": lname}, {"fname": fname, "lname": lname}, {"fname": fname}, {"DOB": dob}):
        res = ctx.find_patients(**kw) or []
        for p in res:
            seen[str(p.get("pid") or p.get("uuid"))] = p
    for p in seen.values():
        if _norm(p.get("fname")) == _norm(fname) and _norm(p.get("lname")) == _norm(lname):
            pd = str(p.get("DOB") or "")[:10]
            if pd == dob:
                return _ns("A chart for this name and date of birth already exists (pid %s)." % p.get("pid"))
            if _typo_dob(pd, dob) and _norm(p.get("street")) == _norm(street):
                return _ns("A chart with the same name and address and a near-identical date of birth already exists (pid %s)." % p.get("pid"))
    ctx.log("no existing chart found")

    def fill(sel, val):
        loc = form.locator(sel)
        loc.click()
        page.keyboard.press("Control+A")
        page.keyboard.type(val)

    def pick(sel, val):
        form.locator(sel).click()
        page.keyboard.type(val)
        page.keyboard.press("Enter")

    if title:
        pick("#form_title", title)
    fill("#form_fname", fname)
    fill("#form_lname", lname)
    fill("#form_DOB", dob)
    if sex:
        pick("#form_sex", sex)
    page.mouse.wheel(0, 300)
    form.get_by_text("Contact", exact=True).first.click()
    page.mouse.wheel(0, 600)
    if street: fill("#form_street", street)
    if city: fill("#form_city", city)
    if state_label: pick("#form_state", state_label)
    if postal: fill("#form_postal_code", postal)
    if phone: fill("#form_phone_home", phone)
    if email: fill("#form_email", email)
    if country: pick("#form_country_code", country)

    form.locator("#create").click()
    deadline = time.time() + 15
    popup = None
    text = ""
    while time.time() < deadline:
        popup = None
        for fr in page.frames:
            try:
                if fr.locator("#confirmCreate").count():
                    popup = fr
                    break
            except Exception:
                pass
        if popup is not None:
            try:
                text = re.sub(r"\s+", " ", popup.locator("body").inner_text(timeout=1000))
            except Exception:
                text = ""
            if "No matches were found" in text or re.search(r"\d+ - \d+ of [1-9]\d* Hits", text):
                break
        time.sleep(0.3)
    if popup is None:
        return _ns("UNEXPECTED: duplicate-check popup with confirm button not found.")
    ctx.log("popup: " + popup.url + " " + text[:300])
    if "No matches were found" not in text:
        if re.search(r"\d+ - \d+ of [1-9]\d* Hits", text):
            return _ns("OpenEMR reports a matching patient may already exist; no chart created.")
        return _ns("UNEXPECTED: duplicate-check popup did not finish loading.")
    btn = popup.locator("#confirmCreate")
    popup.wait_for_load_state("load")
    btn.wait_for(state="visible", timeout=10000)
    time.sleep(1)
    btn.click()
    if not ctx.commit:
        return _ns("DRY RUN reached confirm")
    time.sleep(3)
    res = ctx.find_patients(fname=fname, lname=lname, DOB=dob) or []
    res = [p for p in res if _norm(p.get("fname")) == _norm(fname) and _norm(p.get("lname")) == _norm(lname) and str(p.get("DOB"))[:10] == dob]
    if len(res) == 1:
        return {"result": "SAVED", "reason": "Chart created and read back."}
    return _ns("UNEXPECTED: read-back found %d matching charts." % len(res))
