<div align="center">

# OpenAdapt

**Verify, compile, and replay any GUI workflow or AI agent, with signed Ed25519 receipts on every write.**

[![PyPI](https://img.shields.io/pypi/v/openadapt.svg)](https://pypi.org/project/openadapt/)
[![Chrome Web Store](https://img.shields.io/badge/Chrome%20Web%20Store-OpenAdapt%20Co--Pilot-4285F4?logo=googlechrome&logoColor=white)](https://chromewebstore.google.com/detail/openadapt-co-pilot/bcpliibliddclnibacbjbmbpgffkjefd)
[![Hugging Face Veto-4B-r6](https://img.shields.io/badge/Hugging%20Face-Veto--4B--r6-FFD21E?logo=huggingface&logoColor=black)](https://huggingface.co/OpenAdapt/openadapt-veto-4b-r6)
[![License: MIT](https://img.shields.io/badge/license-MIT-3e6b4f.svg)](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-openadapt.ai-0ea5e9)](https://docs.openadapt.ai)

[Website](https://openadapt.ai) · [Docs](https://docs.openadapt.ai) · [Chrome Extension](https://chromewebstore.google.com/detail/openadapt-co-pilot/bcpliibliddclnibacbjbmbpgffkjefd) · [Watch Real Runs](https://app.openadapt.ai/demo) · [Proof](https://openadapt.ai/proof) · [Discord](https://discord.gg/yF527cQbDG)

</div>

---

OpenAdapt wraps browser and desktop agents with before-click verification, post-write read-back, deterministic compiled replay, and signed Ed25519 receipts.

## Quickstart

### Install

```bash
# Run directly with uvx (no install, fetches Python 3.12 if needed)
uvx --python 3.12 openadapt@latest quickstart

# Or install the CLI
pip install --upgrade openadapt
# macOS / Linux via Homebrew
brew install openadaptai/tap/openadapt
```

For browser workflows with zero CLI setup, install the 1-click Chrome extension: **[OpenAdapt Co-Pilot on the Chrome Web Store](https://chromewebstore.google.com/detail/openadapt-co-pilot/bcpliibliddclnibacbjbmbpgffkjefd)**.

### Wrap Any Python Agent or Tool in 2 Lines

Add pinned record checks, duplicate-submit protection on uncertain writes, same-screen verification, and SHA-256 or Ed25519 receipts to any Python function, Playwright helper, MCP tool, or agent with zero YAML files:

```python
import openadapt

safe_save = openadapt.wrap(
    save_chart_note,
    pinned={"patient_id": "PT-104"},
    check=lambda out, *a, **kw: out.get("saved") is True,
)
safe_save(patient_id="PT-104", note="Follow-up scheduled")
print(safe_save.last_receipt["outcome"], safe_save.last_receipt["checked_how"])  # confirmed same_screen
```

### Try It in 30 Seconds

Run the local smoke test against a bundled demo server (no account or API key required), or trigger a deliberate drift to inspect a halted run and its signed receipt:

```bash
uvx --python 3.12 openadapt@latest quickstart --break-it
```

### Wrap an Agent or Serve Compiled Workflows over MCP

```bash
# Wrap any agent with before-click checks and signed receipts
openadapt wrap --task task.yaml -- claude -p "submit the invoice"

# Expose verified workflows to Claude Code, Codex, or Cursor as a local MCP server
openadapt agent serve

# Serve JIT-compiled plans as zero-token stdio MCP tools
openadapt mcpify serve --from-compiled .openadapt/compiled
```

### Record, Compile, and Replay

```bash
openadapt record --backend web --url https://staging.your-app.example --out rec
openadapt compile rec --out bundle --name my-first-workflow
openadapt flow replay bundle --url https://staging.your-app.example --run-dir run
```

`flow replay` performs every recorded action, including real saves. Use a test or staging copy; it does not ask for approval or check the saved record. For production writes, certify, qualify, and admit the bundle, then run `openadapt run` ([Your first workflow](https://docs.openadapt.ai/tutorials/first-workflow/)).

## Why OpenAdapt

- **Commit-Boundary Verification:** Checks active record identity before consequential clicks, checks observable evidence after each action (on the current screen by default, including in-place edits and non-idempotent portals that cannot reload, or via a reopened view, separate read-only session, or direct API/database/file check when available), and stamps how each action was checked (`same_screen`, `reopened`, `separate_session`, or `system_of_record`) on a signed Ed25519 receipt ([inspect a live receipt](https://openadapt.ai/receipts/3fb27257dbc65e58), [receipt reference](https://docs.openadapt.ai/reference/receipts/)).
- **Zero-API-Call Compiled Replay (No `429` Limits):** Compiles repeated GUI workflows into deterministic programs and direct HTTP or MCP tools, eliminating warm model calls and rate limits (`219 -> 10` calls on Gemini 3.1 Pro).
- **Stage-Only & Human Sign-Off in Your Browser (Zero-PHI Local Execution):** Runs inside existing authenticated Chrome sessions via [OpenAdapt Co-Pilot](https://chromewebstore.google.com/detail/openadapt-co-pilot/bcpliibliddclnibacbjbmbpgffkjefd) or local and VPC runners. Screens, typed values, and recordings stay on your machine, and uncertain writes pause for a human to review and approve.
- **Local 4B Verifier (`OpenAdapt-Veto-4B-r6`):** Open-weights 4B local safety verifier on [Hugging Face](https://huggingface.co/OpenAdapt/openadapt-veto-4b-r6) that checks screenshots locally in 3.15 GB VRAM (`357 ms` latency) before clicks land.

## Verified Results

- **0% silent errors** across 60 real upstream web app upgrades (Gitea, Vikunja, Memos, Mealie), with 60/60 signed Ed25519 receipts.
- **0 warm API calls and no `429` rate limits** once compiled (`219 -> 10` calls on Gemini 3.1 Pro; warm compiled runs execute in `1.88 s` at `$0.000015/run`).
- **5x fewer false alarms** on Multi-Tab Stanford `HealthAdminBench` (`4.2%` vs. `20.8%` false escalations, lifting completion to `50.0%` with 0 regressions).
- **0 / 96 prompt injections** succeeded on `AgentDojo`.

Full methodology, benchmark tables, and verifiable raw runs: **[openadapt.ai/proof](https://openadapt.ai/proof)** and sanitized per-run JSON ledgers in [`benchmarks/public-runs/`](benchmarks/public-runs/).

## Works With

- **Coding & MCP agents:** Claude Code (`claude mcp add openadapt -- uvx --python 3.12 openadapt@latest agent serve`), Codex (`openadapt wrap -- codex ...`), Cursor.
- **Browser automation & sandboxes:** Playwright, Browser Use, Browserbase, E2B, Daytona (`openadapt wrap --cdp-url "$CDP_URL" --upload -- python my_agent.py`). See the [cloud sandbox and BYOC guide](https://docs.openadapt.ai/how-to/verify-cloud-sandboxes/).
- **In-browser operator workflows:** [OpenAdapt Co-Pilot Chrome Extension](https://chromewebstore.google.com/detail/openadapt-co-pilot/bcpliibliddclnibacbjbmbpgffkjefd).

## Packages & Architecture

`pip install openadapt` installs the CLI, compiler/runtime engine, and MCP server. Each package installs only what it imports ([install weight policy](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/docs/monorepo/INSTALL_WEIGHT.md)).

| Package | Role | Source |
|---|---|---|
| [`openadapt`](https://pypi.org/project/openadapt/) | Unified CLI (`quickstart`, `wrap`, `connect`, `record`, `compile`, `run`, `verify`, `kernel`, `agent`, `mcpify`) | [`openadapt/`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/openadapt) |
| [`openadapt-kernel`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/kernel) | Commit-boundary kernel: default-deny write ledger, wire/DOM commit interceptor, stepwise live-DOM healer, cross-rail oracles, Ed25519 receipts, stdio MCP | [`packages/kernel/`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/kernel) |
| [`openadapt-flow`](https://pypi.org/project/openadapt-flow/) | Demonstration compiler, certification, qualification, governed replay, auto-repair, and BYOC connector | [`packages/flow/`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/flow) |
| [`openadapt-agent`](https://pypi.org/project/openadapt-agent/) | MCP server, Agent Skills export, and A2A handoffs | [`packages/agent/`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/agent) |
| [`openadapt-verify`](https://pypi.org/project/openadapt-verify/) | Standalone Python SDK (`@verified`, `checked_write`, local JIT compiler, `mcpify`) | [`packages/verify/`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/verify) |
| [`openadapt-copilot`](https://chromewebstore.google.com/detail/openadapt-co-pilot/bcpliibliddclnibacbjbmbpgffkjefd) | Chrome Extension for recording, stage-only review, and human sign-off inside existing browser tabs | [`packages/flow/chrome_extension/`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/flow/chrome_extension) |

Supporting packages in this monorepo: [`openadapt-desktop`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/desktop), [`openadapt-types`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/types), [`openadapt-capture`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/capture), [`openadapt-privacy`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/privacy), and [`openadapt-tray`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/tray). Research repositories live separately: [openadapt-evals](https://github.com/OpenAdaptAI/openadapt-evals) and [openadapt-ml](https://github.com/OpenAdaptAI/openadapt-ml).

## Documentation

- [Documentation home](https://docs.openadapt.ai)
- [Your first workflow](https://docs.openadapt.ai/tutorials/first-workflow/)
- [Run outcomes](https://docs.openadapt.ai/reference/outcomes/) and [signed receipts](https://docs.openadapt.ai/reference/receipts/)
- [Surfaces](https://docs.openadapt.ai/explanation/surfaces/) and [limits](https://docs.openadapt.ai/explanation/limits/)
- [Security and procurement](https://docs.openadapt.ai/security/) (local-first data boundary, subprocessors, release integrity)
- [Pre-1.0 legacy archive (`v0.46.0`)](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/docs/LEGACY_FREEZE.md)

<details>
<summary>Release status</summary>

The block below is generated from the signed public lifecycle record.

<!-- BEGIN PRODUCTION LIFECYCLE -->
The last signed lifecycle snapshot (September 2026) has seven active target admissions.
Their evidence class is `remote-safe-synthetic`, not MockMed `production_acceptance`.
They remain valid until revoked; there's no expiry date. The admitted versions in that
snapshot are launcher 1.16.0, Flow 1.35.1, and Desktop's 0.16.0 wheel (`pip install openadapt`
installs the latest PyPI release; see [GitHub Releases](https://github.com/OpenAdaptAI/OpenAdapt/releases)
and [PyPI](https://pypi.org/project/openadapt/) for current package versions, which have not
yet been re-admitted to the signed ledger). Native Mac DMGs from that snapshot stay ad-hoc
signed 0.15.0; don't treat those as admitted. A Production run still needs a workflow admission
for the exact compiled bundle, application, and environment.
[Check the live signed Production record](https://docs.openadapt.ai/production-lifecycle.json).
<!-- END PRODUCTION LIFECYCLE -->

</details>

## Contributing & License

Launcher, kernel, engine, MCP server, desktop app, and contract changes live in this monorepo.

[Contribution guide](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/CONTRIBUTING.md) · [Issues](https://github.com/OpenAdaptAI/OpenAdapt/issues) · [Discussions](https://github.com/OpenAdaptAI/OpenAdapt/discussions) · [Report a vulnerability](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/SECURITY.md) · [Code signing policy](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/CODE_SIGNING_POLICY.md)

Maintained by [OpenAdaptAI](https://github.com/OpenAdaptAI) under the [MIT License](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/LICENSE).
