# openadapt-kernel

Minimal, self-contained runtime kernel (`< 2,600` LOC, zero `openadapt-flow` dependencies, zero per-app code) implementing commit-boundary verification, cross-rail read-back, macro staging, stepwise live-DOM healing, and shadow-to-autopilot execution:

1. **Default-Deny Obligation, Capability, and Saga Ledger (`ledger.py`):** Episodes start `UNVERIFIED` and emit `VERIFIED` only when every obligation has an independent post-commit read-back proof and zero dirty form buffers remain. Step-limit timeouts, crashes, and unverified exits deterministically return `ESCALATED`. Carries both cross-screen `<MANDATORY_CONSTRAINTS>` and `<VERIFIED_CAPABILITIES>` (user consent and SSA opaque-ID provenance), with durable Saga checkpointing across multi-system workflows.
2. **Wire and DOM-Effect Commit Interceptor (`interceptor.py`):** Detects commits from actual network mutations (`POST`, `PUT`, `PATCH`, `DELETE`) or non-empty dirty form submissions instead of button-label regexes. Excludes search/filter inputs from dirty buffers and escapes escalation payloads safely.
3. **Full-DOM Macro Staging and Stepwise Live-DOM Healer (`macro.py`):** Extracts all interactive controls across the full scrollable page (`observe_schema`), fills an entire form in one pre-commit batch with SSA provenance enforcement (`stage_form`), and heals drifted UI plans step-by-step against the live mounted DOM (handling modals and dialogs that only exist after earlier clicks).
4. **Cross-Rail Read-Back Oracles (`oracles.py`):** Independent read-only SQLite queries (`mode=ro`), pure-stdlib PDF content-stream extraction (filtering `3 Tr` / `7 Tr` invisible text), and multi-tab / fresh-reload DOM verification.
5. **Shadow-to-Autopilot, 4-Mutant Offline Qualification, Micro-Handoff, Live Runner, and MCP Server (`copilot.py`, `extension_bridge.py`, `runner.py`, `mcp_server.py`, `cli.py`):** Induces zero-PII `PortalPack` specs from live Chrome Co-Pilot `extension_evidence.jsonl` recordings, qualifies verifiers offline against four counterfactual mutants (zero staging DB), runs `Draft-to-Commit` or `Verified Autopilot` across main and child popup windows via Playwright or `--cdp-url`, signs `openadapt.kernel.receipt/v1` Ed25519 receipts, and serves tools over stdio JSON-RPC 2.0 MCP.

## CLI Quickstart

```bash
openadapt-kernel shadow-sync --dir ./recordings --pack-id portal-v1 --out pack.json
openadapt-kernel qualify --pack pack.json --evidence ./recordings/run_1/extension_evidence.jsonl
openadapt-kernel keygen --priv priv.pem --pub pub.pem
openadapt-kernel run --pack pack.json --dossier dossier.json --mode draft --cdp-url http://127.0.0.1:9222
openadapt-kernel run --pack pack.json --dossier dossier.json --mode autopilot --url https://portal.example --commit-selector '[data-testid="submit-btn"]' --priv priv.pem --receipt-out receipt.json
openadapt-kernel verify-receipt --receipt receipt.json --pub pub.pem
openadapt-kernel serve-mcp
```
