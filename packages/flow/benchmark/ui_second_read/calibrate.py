"""Measure how long an app's search view lags a save, for ``index_lag_s``.

Pre-registered in PREREG.md: three saves on records that aren't case records;
the longest time until the list view shows the save, times 1.5, rounded up to
whole seconds. Runs through the active chaos profile, if any.

    python -m benchmark.ui_second_read.calibrate --app dolibarr
"""

from __future__ import annotations

import argparse
import json
import math
import time

from benchmark.ui_second_read.common import env, profile, token


def _reader(app_mod, browser):
    s = app_mod.spec()
    page = browser.new_context().new_page()
    page.goto(s.sign_in.url)
    page.fill(s.sign_in.username_selector, env(s.sign_in.username_env))
    page.fill(s.sign_in.password_selector, env(s.sign_in.password_env))
    page.click(s.sign_in.submit_selector)
    page.wait_for_selector(s.sign_in.signed_in_selector, state="attached")
    return s, page


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--app", choices=["openemr", "dolibarr"], required=True)
    args = p.parse_args()
    if args.app == "openemr":
        from benchmark.ui_second_read import app_openemr as mod
    else:
        from benchmark.ui_second_read import app_dolibarr as mod
    lags = []
    with mod.App(None) as app:
        s, reader = _reader(mod, app.browser)
        for _ in range(3):
            if args.app == "dolibarr":
                name, alias = f"UI2R Cal {token()}", f"Alias {token()}"
                url = s.record_url.format(name=name.replace(" ", "%20"))
                app.create(name, alias)
                want = alias
            else:
                pid, _pubpid, _name = mod.seed("555-0000")
                want = f"555-{token(4)}"
                url = s.record_url
                app.set_phone(pid, want)
            saved = time.monotonic()
            while time.monotonic() - saved < 60:
                try:
                    reader.goto(url, wait_until="domcontentloaded")
                    reader.wait_for_selector(s.ready_selector, state="attached",
                                             timeout=20_000)
                    if want in reader.inner_text("body"):
                        break
                except Exception:  # noqa: BLE001 - a chaos error; poll again
                    pass
                time.sleep(0.5)
            lags.append(round(time.monotonic() - saved, 2))
            if args.app == "openemr":
                mod.cleanup()
    lag = math.ceil(max(lags) * 1.5)
    print(json.dumps({"app": args.app, "network": profile(), "lags_s": lags,
                      "index_lag_s": lag}))


if __name__ == "__main__":
    main()
