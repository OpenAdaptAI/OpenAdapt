"""Multilingual and adversarial generalization tests for openadapt-kernel."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from playwright.sync_api import sync_playwright

from openadapt_kernel import (
    BijectiveSymbolCodec,
    ControlSchema,
    DossierVariableSchema,
    EgressPolicy,
    LocalEgressGuard,
    SpanAnchor,
    StatefulTwinApp,
    TwinPageSpec,
    TwinRouteSpec,
    compile_demo_traces_to_cartridge,
    compile_policy_rules_to_table,
    detect_opaque_visual_surfaces,
    discover_case_evidence_tabs,
    discover_taint_free_ui_chrome,
    evaluate_cartridge_ood_support,
    extract_dossier_facts,
    incorporate_prepare_resolution_into_cartridge,
    inspect_outbound_payload,
    is_ambiguous_numeric_date,
    observe_dom_schema,
    parse_canonical_currency_cents,
    parse_canonical_date,
    probe_reversible_evidence_controls_in_twin,
    tokenize_role_label,
    verify_semantic_slot_bindings,
    verify_span_anchor,
)
from openadapt_kernel.macro import _resolve_locator


def test_bijective_symbol_codec_german_sap_and_japanese_clinic_k0() -> None:
    codec = BijectiveSymbolCodec()
    german_html = '<form><label>Prüfgebühr</label><button aria-label="Prüfprotokoll">Buchung prüfen</button></form>'
    jp_html = '<form><label>生年月日</label><button aria-label="添付書類">送信</button></form>'

    sym_de_html = codec.symbolize_html(german_html)
    sym_de_goal = codec.symbolize_text("Öffne Prüfprotokoll und prüfe Prüfgebühr")
    sym_jp_html = codec.symbolize_html(jp_html)
    sym_jp_goal = codec.symbolize_text("生年月日 を確認して 添付書類 を開く")

    # Consistent bijective mapping across HTML and goal text at k=0
    assert "Prüfprotokoll" not in sym_de_html
    assert "生年月日" not in sym_jp_html
    assert codec.symbolize_token("Prüfprotokoll") in sym_de_html
    assert codec.symbolize_token("Prüfprotokoll") in sym_de_goal
    assert codec.symbolize_token("生年月日") in sym_jp_html
    assert codec.symbolize_token("生年月日") in sym_jp_goal

    # Passes strict zero-English-domain EgressPolicy (W3C HTML/ARIA + bijective SYM_* only)
    from openadapt_kernel.egress_guard import _HTML_ARIA_STRUCTURAL_VOCAB

    minimal_policy = EgressPolicy(
        allowed_tokens=_HTML_ARIA_STRUCTURAL_VOCAB,
        include_html_aria_vocab=True,
        include_clinical_vocab=False,
    )
    for payload in (sym_de_html, sym_de_goal, sym_jp_html, sym_jp_goal):
        verdict = inspect_outbound_payload({"html": payload}, minimal_policy)
        assert verdict.allowed is True, f"Blocked symbolized payload: {verdict.rejected_tokens}"

    # Raw unsymbolized German/Japanese text is blocked
    assert inspect_outbound_payload({"raw": "Prüfprotokoll"}, minimal_policy).allowed is False
    assert inspect_outbound_payload({"raw": "生年月日"}, minimal_policy).allowed is False

    # Detokenize recovers local strings inside 127.0.0.1
    assert "Prüfprotokoll" in codec.detokenize_text(sym_de_goal)
    assert "生年月日" in codec.detokenize_text(sym_jp_goal)


def test_taint_free_chrome_blocks_specialty_clinic_cohort_leakage() -> None:
    sessions = {
        "subj_1": ["Dashboard", "Save", "Methadone Maintenance", "Dr. Sarah Chen", "ClinicAOnly"],
        "subj_2": ["Dashboard", "Save", "Methadone Maintenance", "Dr. Sarah Chen", "ClinicAOnly"],
        "subj_3": ["Dashboard", "Save", "Methadone Maintenance", "Dr. Sarah Chen", "ClinicAOnly"],
        "subj_4": ["Dashboard", "Save", "Methadone Maintenance", "Dr. Sarah Chen"],
    }
    cohorts = {"subj_1": "clinic_a", "subj_2": "clinic_a", "subj_3": "clinic_a", "subj_4": "clinic_b"}
    tainted_api_records = ["Methadone Maintenance", "Dr. Sarah Chen"]

    chrome = discover_taint_free_ui_chrome(
        sessions,
        tainted_record_values=tainted_api_records,
        cohort_by_subject=cohorts,
        min_subjects=3,
        min_cohorts=2,
    )
    assert "dashboard" in chrome
    assert "save" in chrome
    # Tainted dynamic record tokens are permanently disqualified despite 4/4 sessions and 2 cohorts
    assert "methadone" not in chrome
    assert "maintenance" not in chrome
    assert "sarah" not in chrome
    assert "chen" not in chrome
    # Single-cohort token disqualified by min_cohorts=2
    assert "clinicaonly" not in chrome


def test_structural_multilingual_evidence_discovery_and_graphql_reversibility_in_twin(tmp_path: Path) -> None:
    controls = [
        {"role": "tab", "label": "Prüfprotokoll"},
        {"tag": "summary", "label": "添付書類"},
        {"tag": "button", "aria-controls": "panel-history", "label": "Verlauf"},
        {"tag": "button", "type": "submit", "label": "Absenden"},
        {"role": "menuitem", "label": "Abmelden"},
    ]
    found = discover_case_evidence_tabs(controls, enable_english_keyword_fallback=False)
    labels = {a.label for a in found}
    assert labels == {"Prüfprotokoll", "添付書類", "Verlauf"}

    # Headless Playwright test: GraphQL POST-for-read tab vs mutating POST submit in StatefulTwinApp
    twin_html = """<!doctype html><html><body>
      <form><input name="note" value="draft-value" /></form>
      <div role="tablist">
        <button id="tab-main" role="tab" aria-selected="true">Main</button>
        <button id="tab-gql" role="tab" aria-selected="false">Prüfprotokoll</button>
      </div>
      <button id="btn-commit" type="button">Commit</button>
      <div id="panel">Initial panel text</div>
      <script>
        document.getElementById('tab-main').onclick = () => {
          document.getElementById('panel').innerText = 'Initial panel text';
        };
        document.getElementById('tab-gql').onclick = () => {
          fetch('/graphql', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({query: '{ auditTrail { id } }'})});
          document.getElementById('panel').innerText = 'Initial panel text plus newly revealed audit evidence';
        };
        document.getElementById('btn-commit').onclick = () => {
          fetch('/api/commit', {method:'POST', body: 'confirm=true'});
          document.getElementById('panel').innerText = 'Committed';
        };
      </script>
    </body></html>"""
    twin = StatefulTwinApp(
        base_url="http://twin.local",
        pages={"/": TwinPageSpec("/", twin_html)},
        route_specs=(
            TwinRouteSpec("POST", "/graphql", {"data": {"auditTrail": []}}, status_code=200),
            TwinRouteSpec("POST", "/api/commit", {"status": "ok"}, mutates_collection="records", status_code=200),
        ),
        egress_guard=LocalEgressGuard(tmp_path / "twin_egress.jsonl"),
    )
    certs = probe_reversible_evidence_controls_in_twin(twin, ("#tab-gql", "#btn-commit"), page_path="/")
    by_sel = {c.selector: c for c in certs}
    assert by_sel["#tab-gql"].certified is True
    assert by_sel["#tab-gql"].information_gain is True
    assert by_sel["#tab-gql"].state_involution_ok is True
    assert any("graphql-read" in r for r in by_sel["#tab-gql"].readonly_requests)
    assert by_sel["#btn-commit"].certified is False
    assert by_sel["#btn-commit"].reason == "mutating_network_request"


def test_compositor_modal_shadow_piercing_nested_iframe_and_canvas_escalation() -> None:
    html = """<!doctype html><html><head><style>
      header { position: fixed; top: 0; left: 0; right: 0; height: 48px; background: #eee; }
      #bg-btn { position: fixed; top: 260px; left: 500px; width: 140px; height: 40px; }
      #custom-modal {
        position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
        width: 480px; height: 320px; background: white; border: 1px solid #333; z-index: 20;
      }
    </style></head><body>
      <header><button id="hdr-btn">Menu</button></header>
      <button id="bg-btn">Background Action</button>
      <div id="custom-modal">
        <input name="modal_code" aria-label="Modal Code" />
      </div>
      <div id="shadow-host"></div>
      <iframe name="child_frame" srcdoc='<form><input name="nested_code" aria-label="Nested Code" /></form>'></iframe>
      <canvas id="rdp-view" width="400" height="300"></canvas>
      <script>
        const host = document.getElementById('shadow-host');
        const sr = host.attachShadow({mode: 'open'});
        sr.innerHTML = '<input name="shadow_field" aria-label="Shadow Field" />';
      </script>
    </body></html>"""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1200, "height": 800})
            page.set_content(html, wait_until="load")
            controls = observe_dom_schema(page)

            by_name = {c.name or c.element_id: c for c in controls}
            assert by_name["hdr-btn"].in_modal is False
            assert by_name["modal_code"].in_modal is True
            assert by_name["bg-btn"].occluded is True
            assert by_name["shadow_field"].in_shadow is True
            assert "#shadow-host >>" in by_name["shadow_field"].selector
            assert by_name["nested_code"].frame_chain == ('iframe[name="child_frame"]',)

            # Cross-frame locator resolution via frame_locator works cleanly
            loc = _resolve_locator(page, by_name["nested_code"])
            loc.fill("NESTED-42")
            assert loc.input_value() == "NESTED-42"

            # Large zero-child <canvas> escalates to visual grounding
            vis = detect_opaque_visual_surfaces(page)
            assert vis["requires_visual_grounding"] is True
            assert vis["surfaces"][0]["id"] == "rdp-view"
        finally:
            browser.close()


def test_multilingual_canonicalizers_eu_currency_ambiguous_dates_and_cjk_slot_swap() -> None:
    # EU, Swiss, and ISO currencies
    assert parse_canonical_currency_cents("1.234,56 €") == 123456
    assert parse_canonical_currency_cents("€ 1 234,56") == 123456
    assert parse_canonical_currency_cents("CHF 1'234.56") == 123456
    assert parse_canonical_currency_cents("EUR 1234.56") == 123456
    assert parse_canonical_currency_cents("(12,50 €)") == -1250

    # Date order & strict ambiguity check
    assert is_ambiguous_numeric_date("04/05/2026") is True
    assert is_ambiguous_numeric_date("19/04/2026") is False
    assert parse_canonical_date("04/05/2026", date_order="STRICT_UNAMBIGUOUS") is None
    assert parse_canonical_date("19/04/2026", date_order="STRICT_UNAMBIGUOUS") == (2026, 4, 19)
    assert parse_canonical_date("19.04.2026", date_order="STRICT_UNAMBIGUOUS") == (2026, 4, 19)
    assert parse_canonical_date("19 Apr 2026", date_order="STRICT_UNAMBIGUOUS") == (2026, 4, 19)

    # Unicode & CJK semantic slot compatibility blocks Japanese 生年月日 vs 入院日 swap
    assert "生年" in tokenize_role_label("生年月日")
    assert "prüfgebühr" in tokenize_role_label("Prüfgebühr")
    schemas = [
        DossierVariableSchema("$dob", "date", tokenize_role_label("生年月日")),
        DossierVariableSchema("$admit_date", "date", tokenize_role_label("入院日")),
    ]
    controls = [
        ControlSchema("input", "text", "textbox", "生年月日", "", "dob", "", "", False, "#dob", control_index=0),
        ControlSchema("input", "text", "textbox", "入院日", "", "admit", "", "", False, "#admit", control_index=1),
    ]
    ok_rep = verify_semantic_slot_bindings({0: "$dob", 1: "$admit_date"}, schemas, controls)
    assert ok_rep.ok is True
    swapped_rep = verify_semantic_slot_bindings({0: "$admit_date", 1: "$dob"}, schemas, controls)
    assert swapped_rep.ok is False


def test_ood_support_guard_online_refinement_and_span_anchored_autoformalization() -> None:
    initial_demos: List[Dict[str, Any]] = [
        {
            "visited_tabs": ["evidence"],
            "observed_features": {"denial_code": "co_16", "coverage": "active"},
            "chosen_disposition": "resubmit",
            "note_template_slots": ["code"],
            "readback_url": "https://portal.example/claims/10/summary",
            "actions": [{"action": "click"}],
        },
        {
            "visited_tabs": ["evidence"],
            "observed_features": {"denial_code": "co_29", "coverage": "active"},
            "chosen_disposition": "appeal",
            "note_template_slots": ["code"],
            "readback_url": "https://portal.example/claims/11/summary",
            "actions": [{"action": "click"}],
        },
    ]
    carc_policy = EgressPolicy(extra_allowed_tokens=frozenset({"co_16", "co_29", "co_999"}))
    cartridge = compile_demo_traces_to_cartridge(
        initial_demos,
        cartridge_id="ood_cart_v1",
        portal_origin="https://portal.example",
        egress_policy=carc_policy,
    )
    assert "co_16" in cartridge.sop_spec.observed_feature_domains["denial_code"]

    # Unseen denial code triggers OOD PREPARE abstention (stops silent extrapolation)
    ood = evaluate_cartridge_ood_support(cartridge, {"denial_code": "co_999", "coverage": "active"})
    assert ood.in_support is False
    assert ood.requires_prepare is True
    assert ood.unseen_features == {"denial_code": "co_999"}

    # Active online refinement incorporates a single human PrepareCard resolution
    new_demo = {
        "visited_tabs": ["evidence"],
        "observed_features": {"denial_code": "co_999", "coverage": "active"},
        "chosen_disposition": "escalate",
        "note_template_slots": ["code"],
        "readback_url": "https://portal.example/claims/12/summary",
        "actions": [{"action": "click"}],
    }
    updated, qual = incorporate_prepare_resolution_into_cartridge(
        cartridge, new_demo, initial_demos, egress_policy=carc_policy
    )
    assert qual.qualified is True
    in_sup = evaluate_cartridge_ood_support(updated, {"denial_code": "co_999", "coverage": "active"})
    assert in_sup.in_support is True
    assert in_sup.requires_prepare is False

    # Span-anchored autoformalization rejects unanchored/hallucinated LLM facts and policy rules
    src_text = "Richtlinie: Keine Teilgutschrift ohne Freigabe."
    sources = {"doc1": src_text}
    quote = "Keine Teilgutschrift"
    start = src_text.index(quote)
    assert verify_span_anchor(sources, SpanAnchor("doc1", start, start + len(quote), quote)) is True
    assert verify_span_anchor(sources, SpanAnchor("doc1", 0, 5, "WRONG")) is False

    facts = extract_dossier_facts(
        sources,
        enable_english_regex_heuristics=False,
        require_llm_span_anchors=True,
        llm_extractor_fn=lambda _s: [
            {
                "category": "policy_clause",
                "value": quote,
                "span_anchor": {"source_key": "doc1", "start": start, "end": start + len(quote), "quote": quote},
            },
            {
                "category": "policy_clause",
                "value": "hallucinated rule not in text",
                "span_anchor": {"source_key": "doc1", "start": 0, "end": 10, "quote": "hallucinated"},
            },
            {"category": "policy_clause", "value": "unanchored claim"},
        ],
    )
    fact_vals = [f.value for f in facts]
    assert quote in fact_vals
    assert "hallucinated rule not in text" not in fact_vals
    assert "unanchored claim" not in fact_vals

    table, _cfg = compile_policy_rules_to_table(
        [{"rule_id": "de_1", "description": src_text}],
        enable_english_regex_heuristics=False,
        require_llm_span_anchors=True,
        llm_compiler_fn=lambda _specs: {
            "rules": [
                {
                    "rule_id": "anchored_rule",
                    "conditions": {"action": "partial_credit"},
                    "required_disposition": "REFUSE",
                    "span_anchor": {"source_key": "de_1", "start": start, "end": start + len(quote), "quote": quote},
                },
                {
                    "rule_id": "hallucinated_rule",
                    "conditions": {"action": "anything"},
                    "required_disposition": "ALLOW",
                },
            ]
        },
    )
    rule_ids = [r.rule_id for r in table.rules]
    assert rule_ids == ["anchored_rule"]
