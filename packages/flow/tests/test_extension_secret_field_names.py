"""One secret-field name table, run through every extension redaction layer.

Fix-forward for #2575 (Grok's REQUEST CHANGES, issuecomment-7126795048):
camelCase and PascalCase names such as ``patientSsn``, ``cardCvv``,
``userPin`` and ``otpCode`` passed the boundary-anchored regexes, so their
typed values reached the native host in clear.

Each layer must flag the same names:

1. ``chrome_extension/content.js`` ``isSecretField`` (name, id, aria-label,
   autocomplete), run in Node against the fake DOM harness.
2. ``chrome_extension/network_redact.js`` ``redactNetworkSave`` (JSON keys).
3. ``openadapt_flow.extension_evidence.redact_evidence`` (name, id).
4. ``openadapt_flow.extension_network.redact_network_save`` (JSON keys).

The vocabulary lives in ``openadapt_flow/secret_field_vocabulary.json``; the
two JavaScript files embed a copy that must stay identical.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from openadapt_flow.extension_evidence import redact_evidence
from openadapt_flow.extension_network import redact_network_save

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "chrome_extension"
CONTENT = EXT / "content.js"
NETWORK_REDACT = EXT / "network_redact.js"
HARNESS = ROOT / "tests" / "extension_support" / "chrome_extension_harness.js"
VOCABULARY = ROOT / "openadapt_flow" / "secret_field_vocabulary.json"

SECRET_NAMES = [
    "patientSsn",
    "memberSsn",
    "applicantSSN",
    "PatientSSN",
    "SSNLast4",
    "ssnLast4",
    "cardCvv",
    "cvvCode",
    "cardCvc",
    "card_cvv",
    "card-cvv",
    "card.cvv",
    "userPin",
    "pinCode",
    "otpCode",
    "smsCode",
    "oneTimeCode",
    "verificationCode",
    "mfaToken",
    "totpSecret",
    "routingNumber",
    "accountNumber",
    "ibanValue",
    "taxId",
    "socialSecurityNumber",
    "apiKeyValue",
    "refreshToken",
    "AccessToken",
    "authCode",
    "userPasscode",
    "code",
]
CLEAR_NAMES = [
    "pinnedItems",
    "spinnerState",
    "opinionText",
    "productCode",
    "claimId",
]
CARD_CVV_LABEL = "Card CVV"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("Node.js is required to test the Chrome extension scripts")
        pytest.skip("Node.js is not installed")
    return node


def _node_json(args: list[str], *, script: str | None = None) -> object:
    cmd = [_node(), *args] if script is None else [_node(), "-e", script]
    completed = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60, check=False
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout)


def _content_is_secret(attr_sets: list[dict[str, str]]) -> list[bool]:
    steps = [{"isSecret": {"tag": "input", "attrs": attrs}} for attrs in attr_sets]
    results = _node_json([str(HARNESS), "content", str(CONTENT), json.dumps(steps)])
    assert isinstance(results, list)
    return [bool(r["secret"]) for r in results]


# --- content.js ----------------------------------------------------------------


@pytest.mark.parametrize("attr", ["name", "id", "aria-label"])
def test_content_script_flags_camel_case_secret_names(attr: str) -> None:
    flags = _content_is_secret([{attr: n} for n in SECRET_NAMES])
    missed = [n for n, flag in zip(SECRET_NAMES, flags) if not flag]
    assert missed == [], f"content.js {attr} missed {missed}"


def test_content_script_flags_secret_id_behind_benign_name() -> None:
    flags = _content_is_secret(
        [{"name": "note", "id": n} for n in ("patientSsn", "cardCvv", "userPin")]
    )
    assert flags == [True, True, True]


def test_content_script_flags_aria_label_card_cvv() -> None:
    assert _content_is_secret([{"aria-label": CARD_CVV_LABEL}]) == [True]


def test_content_script_flags_secret_autocomplete_tokens() -> None:
    assert _content_is_secret([{"autocomplete": "cc-csc"}, {"autocomplete": "name"}]) == [
        True,
        False,
    ]


def test_content_script_leaves_lookalike_names_clear() -> None:
    attr_sets = [{attr: n} for n in CLEAR_NAMES for attr in ("name", "id", "aria-label")]
    flags = _content_is_secret(attr_sets)
    flagged = [a for a, flag in zip(attr_sets, flags) if flag]
    assert flagged == []


# --- network_redact.js -----------------------------------------------------------


def _js_network_body(body: dict[str, object]) -> dict[str, object]:
    script = f"""
const fs = require('fs');
const vm = require('vm');
const ctx = vm.createContext({{
  URL, URLSearchParams, TextDecoder, TextEncoder, Uint8Array, Date, Set, Map,
  atob: (s) => Buffer.from(s, 'base64').toString('binary'),
}});
ctx.globalThis = ctx;
vm.runInContext(fs.readFileSync({json.dumps(str(NETWORK_REDACT))}, 'utf8'), ctx);
const record = ctx.OpenAdaptNetworkRedact.redactNetworkSave({{
  method: 'POST',
  url: 'https://portal.example/api/submit',
  type: 'xmlhttprequest',
  tabId: 3,
  frameId: 0,
  timeStamp: 1000,
  statusCode: 200,
  requestHeaders: [{{ name: 'Content-Type', value: 'application/json' }}],
  responseHeaders: [],
  requestBody: {{ raw: [{{ bytes: new TextEncoder().encode({json.dumps(json.dumps(body))}) }}] }},
}});
console.log(JSON.stringify(record ? record.request_body : null));
"""
    out = _node_json([], script=script)
    assert isinstance(out, dict), f"redactNetworkSave dropped the record: {out!r}"
    return out


def _body(names: list[str]) -> dict[str, object]:
    return {name: f"typed-value-{i:04d}-{name}" for i, name in enumerate(names)}


def test_js_network_redactor_withholds_camel_case_json_keys() -> None:
    body = _body(SECRET_NAMES)
    out = _js_network_body(body)
    leaked = [k for k in SECRET_NAMES if out.get(k) != "<secret>"]
    assert leaked == [], f"network_redact.js kept {leaked}"


def test_js_network_redactor_withholds_user_pin_json_body() -> None:
    out = _js_network_body({"userPin": "4417", "claim": {"id": 7}})
    assert out == {"userPin": "<secret>", "claim": {"id": 7}}


def test_js_network_redactor_keeps_lookalike_keys() -> None:
    body = _body(CLEAR_NAMES)
    assert _js_network_body(body) == body


# --- extension_evidence.py -------------------------------------------------------


def _evidence(**fields: object) -> dict[str, object]:
    line = {"kind": "target", "tag": "input", "value": "typed-value-123456"}
    line.update(fields)
    return redact_evidence(line)


@pytest.mark.parametrize("field", ["name", "id"])
def test_evidence_ingest_withholds_camel_case_secret_names(field: str) -> None:
    missed = [
        n for n in SECRET_NAMES if _evidence(**{field: n}).get("value") != "<secret>"
    ]
    assert missed == [], f"extension_evidence {field} missed {missed}"


def test_evidence_ingest_withholds_aria_label_name() -> None:
    # content.js sends aria-label as ``name`` when the field has no name.
    out = _evidence(name=CARD_CVV_LABEL)
    assert out["value"] == "<secret>"
    assert out["secret_field"] is True


def test_evidence_ingest_keeps_lookalike_names() -> None:
    for name in CLEAR_NAMES:
        assert _evidence(name=name)["value"] == "typed-value-123456", name


# --- extension_network.py --------------------------------------------------------


def _py_network_body(body: dict[str, object]) -> dict[str, object]:
    record = redact_network_save(
        {
            "kind": "network_save",
            "method": "POST",
            "url": "https://portal.example/api/submit",
            "tab_id": 3,
            "frame_id": 0,
            "status": 200,
            "request_headers": [{"name": "Content-Type", "value": "application/json"}],
            "response_headers": [],
            "request_body_status": "json",
            "request_body": body,
            "redacted": True,
            "t": 1000,
        }
    )
    assert record is not None, "redact_network_save dropped the record"
    out = record["request_body"]
    assert isinstance(out, dict)
    return out


def test_py_network_redactor_withholds_camel_case_json_keys() -> None:
    out = _py_network_body(_body(SECRET_NAMES))
    leaked = [k for k in SECRET_NAMES if out.get(k) != "<secret>"]
    assert leaked == [], f"extension_network kept {leaked}"


def test_py_network_redactor_withholds_user_pin_json_body() -> None:
    out = _py_network_body({"userPin": "4417", "claim": {"id": 7}})
    assert out == {"userPin": "<secret>", "claim": {"id": 7}}


def test_py_network_redactor_keeps_lookalike_keys() -> None:
    body = _body(CLEAR_NAMES)
    assert _py_network_body(body) == body


# --- one shared vocabulary -------------------------------------------------------

_EMBED = re.compile(
    r"// secret-field-vocabulary:begin[^\n]*\n\s*const SECRET_FIELD_PHRASES = "
    r"(\[[\s\S]*?\]);\s*\n\s*// secret-field-vocabulary:end"
)


@pytest.mark.parametrize("script", [CONTENT, NETWORK_REDACT], ids=lambda p: p.name)
def test_javascript_layers_embed_the_shared_vocabulary(script: Path) -> None:
    canonical = json.loads(VOCABULARY.read_text(encoding="utf-8"))["phrases"]
    match = _EMBED.search(script.read_text(encoding="utf-8"))
    assert match, f"{script.name} has no secret-field-vocabulary block"
    assert json.loads(match.group(1)) == canonical, (
        f"{script.name} vocabulary differs from {VOCABULARY.name}; copy it over"
    )


def test_vocabulary_has_no_bare_code_and_covers_the_reported_names() -> None:
    phrases = json.loads(VOCABULARY.read_text(encoding="utf-8"))["phrases"]
    assert "code" not in phrases  # ``^code$`` stays an exact rule only
    for required in ("ssn", "cvv", "pin", "otp", "one time code", "card number"):
        assert required in phrases
