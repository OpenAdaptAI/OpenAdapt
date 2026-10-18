"""Synthetic event streams for shadow-mining tests."""

from __future__ import annotations

import random

from openadapt_capture.shadow import ShadowEvent

# Values that must never appear in any stored form.
SECRET_VALUES = (
    "INV-559017",
    "4471.25",
    "jane.doe@example.com",
    "Jane Doe",
    "123-45-6789",
    "hunter2-password",
    "Quarterly report for Jane Doe",
)


def invoice_episode(t: float, invoice: str, amount: str, *, extra_step: bool = False):
    """Copy an invoice number from a spreadsheet into a web form and submit it."""

    events = [
        ShadowEvent(t, "focus", app="Microsoft Excel"),
        ShadowEvent(t + 1, "click", app="Microsoft Excel", role="AXCell", label=invoice),
        ShadowEvent(t + 2, "copy", app="Microsoft Excel", key="c", modifiers=("meta",)),
        ShadowEvent(t + 3, "focus", app="Google Chrome"),
        ShadowEvent(
            t + 4,
            "navigate",
            app="Google Chrome",
            url=f"https://erp.example.com/invoices/{invoice}?ssn=123-45-6789#Jane",
        ),
        ShadowEvent(t + 5, "click", app="Google Chrome", role="textbox", label="Invoice number"),
        ShadowEvent(t + 6, "paste", app="Google Chrome", key="v", modifiers=("meta",)),
        ShadowEvent(t + 7, "click", app="Google Chrome", role="textbox", label="Amount"),
        ShadowEvent(
            t + 8, "type", app="Google Chrome", role="textbox", label="Amount", text=amount[:2]
        ),
        ShadowEvent(
            t + 8.5, "type", app="Google Chrome", role="textbox", label="Amount", text=amount[2:]
        ),
    ]
    if extra_step:
        events.append(
            ShadowEvent(t + 8.8, "click", app="Google Chrome", role="checkbox", label="Urgent")
        )
    events += [
        ShadowEvent(t + 9, "click", app="Google Chrome", role="AXButton", label="Submit"),
        ShadowEvent(
            t + 10, "submit", app="Google Chrome", url="https://erp.example.com/invoices/new"
        ),
    ]
    return events


def report_episode(t: float, recipient: str, subject: str):
    """Save a spreadsheet, then mail it: fixed controls, varying typed values."""

    return [
        ShadowEvent(t, "focus", app="Microsoft Excel"),
        ShadowEvent(t + 1, "shortcut", app="Microsoft Excel", key="s", modifiers=("meta",)),
        ShadowEvent(t + 2, "focus", app="Mail"),
        ShadowEvent(t + 3, "click", app="Mail", role="AXButton", label="New Message"),
        ShadowEvent(t + 4, "click", app="Mail", role="AXTextField", label="To"),
        ShadowEvent(t + 5, "type", app="Mail", role="AXTextField", label="To", text=recipient),
        ShadowEvent(t + 6, "click", app="Mail", role="AXTextField", label="Subject"),
        ShadowEvent(t + 7, "type", app="Mail", role="AXTextField", label="Subject", text=subject),
        ShadowEvent(t + 8, "click", app="Mail", role="AXButton", label="Attach"),
        ShadowEvent(t + 9, "click", app="Mail", role="AXButton", label="Send"),
    ]


def report_task_stream(rng: random.Random, repeats: int, *, start: float = 5_000_000.0):
    events = []
    t = start
    for r in range(repeats):
        noise, t = random_events(rng, t, 25)
        events += noise
        t += 30
        recipient = SECRET_VALUES[2] if r == 0 else f"person{rng.randint(1, 999)}@example.org"
        subject = SECRET_VALUES[6] if r == 0 else f"Weekly numbers {rng.randint(1, 52)}"
        events += report_episode(t, recipient, subject)
        t += 40
    noise, t = random_events(rng, t, 25)
    return events + noise, t


NOISE_APPS = ("Slack", "Finder", "Mail", "Terminal")
NOISE_ROLES = ("button", "cell", "textfield", "menuitem", "link", "statictext")


def random_events(rng: random.Random, t: float, count: int, *, alphabet: int = 40):
    """Random UI activity over a fixed alphabet of structural symbols."""

    shapes = []
    for i in range(alphabet):
        app = NOISE_APPS[i % len(NOISE_APPS)]
        role = NOISE_ROLES[(i // len(NOISE_APPS)) % len(NOISE_ROLES)]
        shapes.append((app, role, f"Control {chr(65 + i % 26)}{i // 26}"))
    out = []
    for _ in range(count):
        t += rng.uniform(0.5, 20)
        app, role, label = rng.choice(shapes)
        if role == "textfield" and rng.random() < 0.5:
            out.append(
                ShadowEvent(
                    t, "type", app=app, role=role, label=label, text=str(rng.randint(0, 10**6))
                )
            )
        else:
            out.append(ShadowEvent(t, "click", app=app, role=role, label=label))
    return out, t


def repeated_task_stream(
    rng: random.Random,
    repeats: int,
    *,
    start: float = 1_000_000.0,
    noise_between: int = 25,
    variant_at: int | None = None,
):
    events = []
    t = start
    for r in range(repeats):
        noise, t = random_events(rng, t, noise_between)
        events += noise
        t += 30
        invoice = SECRET_VALUES[0] if r == 0 else f"INV-{rng.randint(10**5, 10**6)}"
        amount = SECRET_VALUES[1] if r == 0 else f"{rng.randint(10, 9999)}.{rng.randint(10, 99)}"
        events += invoice_episode(t, invoice, amount, extra_step=(r == variant_at))
        t += 40
    noise, t = random_events(rng, t, noise_between)
    events += noise
    return events, t
