<div align="center">

# OpenAdapt

**Compile, verify, and self-heal any GUI workflow or AI agent, with signed Ed25519 receipts on every write.**

[![CI](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/main.yml/badge.svg)](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/main.yml)
[![Packages CI](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/packages.yml/badge.svg)](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/packages.yml)
[![PyPI](https://img.shields.io/pypi/v/openadapt.svg)](https://pypi.org/project/openadapt/)
[![Python 3.11-3.12](https://img.shields.io/badge/python-3.11--3.12-blue)](https://www.python.org/downloads/)
[![Hugging Face Veto-4B-r6](https://img.shields.io/badge/Hugging%20Face-Veto--4B--r6-FFD21E?logo=huggingface&logoColor=black)](https://huggingface.co/OpenAdapt/openadapt-veto-4b-r6)
[![License: MIT](https://img.shields.io/badge/license-MIT-3e6b4f.svg)](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/LICENSE)
[![Discord](https://img.shields.io/badge/Discord-community-5865F2?logo=discord&logoColor=white)](https://discord.gg/yF527cQbDG)

[Website](https://openadapt.ai) · [Docs](https://docs.openadapt.ai) · [Live Demo](https://app.openadapt.ai/demo) · [Starter Skills Registry](https://app.openadapt.ai/skills) · [Benchmarks](https://openadapt.ai/proof)

</div>

---

Wrap any AI agent with before-click checks, fresh-reload read-back, and signed Ed25519 receipts. On clean-room benchmarks, OpenAdapt cuts silent write errors to **0.0%** (down from 15.0% for unwrapped `gpt-5.4` and 18.3% for ASI skill synthesis), raises multi-tab `HealthAdminBench` completion to **50.0%** with **5x fewer false escalations**, and runs repeating workflows **1,188x cheaper** (`$0.000015/run`) and **5.1x faster** (`1.88 s`) once compiled.

## Quickstart

### Claude Code

Wrap a Claude Code run directly, or register OpenAdapt as a local MCP server so Claude calls verified workflows as typed tools:

```bash
uvx --python 3.12 openadapt@latest wrap -- claude -p "submit the invoice"
```

```bash
claude mcp add openadapt -- uvx --python 3.12 openadapt@latest agent serve
```

### Codex

```bash
uvx --python 3.12 openadapt@latest wrap -- codex "update the claim status"
```

### Python, Browser Use, and Cloud Sandboxes (Browserbase, E2B, Daytona)

Wrap any Python or `browser-use` agent locally, or point `openadapt wrap` at a remote CDP WebSocket URL and stream signed receipts to `app.openadapt.ai`:

```bash
# Local Python or browser-use script
uvx --python 3.12 openadapt@latest wrap -- python my_agent.py

# Remote CDP browser sandbox (Browserbase, E2B, Daytona) with receipt upload
export OPENADAPT_CLOUD_KEY="$YOUR_OPENADAPT_KEY"
uvx --python 3.12 openadapt@latest wrap \
  --task task.json \
  --cdp-url "$BROWSERBASE_CONNECT_URL" \
  --upload \
  -- python my_agent.py
```

See the [cloud sandbox and BYOC guide](https://docs.openadapt.ai/how-to/verify-cloud-sandboxes/) for full Browserbase, Browser Use, E2B, and Daytona snippets.

### 30-Second Local Smoke Test

```bash
uvx --python 3.12 openadapt@latest quickstart
```

No account or API key required. [uv](https://docs.astral.sh/uv/getting-started/installation/) fetches Python 3.12 if needed, and `@latest` runs the newest release. Add `--watch` to watch the browser window, or run `uvx --python 3.12 openadapt@latest quickstart --break-it` to inspect how a halted run and its signed receipt appear in the terminal.

This quickstart is a local developer smoke test against a bundled demo server to confirm your installation and inspect CLI output. It is a developer walkthrough only; all empirical claims come from the clean-room external benchmarks below.

## Clean-Room Benchmarks

All raw tables, methodology, and verifiable Ed25519 receipts are published at [openadapt.ai/proof](https://openadapt.ai/proof).

### 1. Real-Upgrade UI Drift Across 4 Web Apps (`N = 60`, Gitea / Vikunja / Memos / Mealie)

Evaluated across real upstream Docker app upgrades with 100-run cost amortization and `60/60` signed Ed25519 receipts (paired unwrapped `gpt-5.4` subset `N = 20`):

| System | Survival | Silent errors | Amortized cost / run | Warm p50 latency |
|---|---|---|---|---|
| **OpenAdapt (`CommitJIT`)** | **70.0%** (42/60) | **0.0%** (0/60) | **$0.000015** (1,188x cheaper than `gpt-5.4`) | **1.88 s** (5.1x faster) |
| ASI Programmatic Skill Synthesis | -- | 18.3% (0.0% with OpenAdapt, `p = 0.000977`) | -- | -- |
| Unwrapped `gpt-5.4` (paired `N = 20`) | 80.0% (16/20) | 15.0% (3/20 -> 0.0% with OpenAdapt) | $0.017824 | 9.53 s |
| Stagehand-style unverified self-heal | 71.7% (43/60) | 3.3% (2/60 -> 0.0% with OpenAdapt) | $0.000205 | 4.47 s |
| Static Playwright | 6.7% (4/60) | -- | -- | -- |
| Prompt-AWM (ICML 2025) | 1.7% (1/60) | -- | -- | -- |

### 2. Zero-Leak Stanford `HealthAdminBench` (`N = 24` paired, `gpt-5.4` + `claude-sonnet-5-5`)

| Evaluation mode | Metric | Baseline | With OpenAdapt | Delta |
|---|---|---|---|---|
| **Multi-Tab** | Full completion | 41.7% | **50.0%** | **+8.3 pp** |
| **Multi-Tab** | Verified completion | 41.7% | **45.8%** | **+25.0 pp** vs. single-tab (`20.8%`) |
| **Multi-Tab** | False escalations | 20.8% | **4.2%** | **5x reduction** (`0` regressions) |
| **Single-Tab** (132 real reloads) | Silent errors | 70.8% | **45.8%** | **-25.0 pp** (`p = 0.031`, `0` regressions) |
| **Single-Tab** (132 real reloads) | Full completion | 29.2% | **41.7%** | **+12.5 pp** |

### 3. `Veto-4B-r6` Local 4B Safety Sidecar

Open-weights 4B local safety verifier published on Hugging Face ([`OpenAdapt/openadapt-veto-4b-r6`](https://huggingface.co/OpenAdapt/openadapt-veto-4b-r6)), evaluated out-of-distribution on 4 unseen desktop apps:

| Metric | Base 4B VLM | `Veto-4B-r6` | Notes |
|---|---|---|---|
| OOD AUROC | 0.627 | **0.791** | `p = 0.0051` on 4 unseen desktop apps |
| OOD False Positive Rate | 37.9% | **6.9%** | `357 ms` latency, `3.15 GB` VRAM |

### 4. Production EHR & Claims Timing (OpenEMR & openIMIS)

On a local OpenEMR 7.0/8.0 test copy (new-patient referral from fresh browser through sign-in and database read-back, median):

| Runner | Median time | Saved correctly | Model cost |
|---|---|---|---|
| Claude Sonnet 5.5 computer use | 36.5 s | 62 of 63 | ~$0.08 |
| OpenAdapt compiled program (screen steps) | 22.5 s | 18 of 18 | $0 |
| OpenAdapt compiled program (compiled API step) | **3.3 s** | 18 of 18 | **$0** |

On a local openIMIS test copy, the compiled program entered 149 synthetic claims with zero model calls: 148 read back correct, 0 saved wrong, and 1 stopped before saving ([SCOREBOARD.md](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/packages/flow/benchmark/scoreboard/SCOREBOARD.md)).

## How It Works

<p align="center">
  <img src="https://raw.githubusercontent.com/OpenAdaptAI/OpenAdapt/main/media/openadapt-hero.svg" alt="OpenAdapt architecture: record or wrap an agent, compile into a deterministic program, verify before and after each write, and halt with a signed receipt on drift" width="100%">
</p>

### 3 Execution Modes

| Mode | Command / Entry | Best for |
|---|---|---|
| **Local / BYOC Runner** | `openadapt connect --token "$BYOC_CONNECTOR_TOKEN"` | Private VPCs, Windows/macOS/Linux hosts, and internal EHR or ERP apps behind firewalls (outbound HTTPS only; `--print-service` outputs Windows Service, systemd, and Docker templates) |
| **OpenAdapt Hosted Web** | `openadapt wrap publish --task task.json` | Running admitted web workflows and viewing signed receipts in [app.openadapt.ai](https://app.openadapt.ai) |
| **Bring Your Own Sandbox** | `openadapt wrap --cdp-url "$CDP_URL" --upload -- <cmd>` | Wrapping existing agents inside Browserbase, Browser Use, E2B, or Daytona containers while keeping cloud keys withheld from the agent |

### 3-Level Verification Ladder

OpenAdapt picks the strongest check the target application supports:

- **API / MCP Promotion:** Compiles repeated GUI form submissions into direct HTTP/RPC calls and typed MCP tools (`openadapt agent serve`), cutting latency ~7x (3.3 s vs. 22.5 s on OpenEMR).
- **Level 1 (Before-click local guard, 0 DB required):** Verifies active record identity across tabs, enforces policy and consent gates before consequential writes, and runs optional local `Veto-4B-r6` screenshot checks. Stops and asks a person on ambiguity.
- **Level 2 (Post-action fresh-reload read-back, 0 DB required):** Reloads or opens a separate session to read back submitted fields, server confirmation IDs, or generated PDFs.
- **Level 3 (Direct API or DB read-back, optional upgrade):** Reads the system of record directly over read-only FHIR, REST, or SQL and signs a `VERIFIED` Ed25519 receipt.

## Packages

`pip install openadapt` installs the launcher, compiler/runtime engine, and MCP server. Every package installs only what it imports ([install weight policy](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/docs/monorepo/INSTALL_WEIGHT.md)).

| Package | Role | Source |
|---|---|---|
| [`openadapt`](https://pypi.org/project/openadapt/) | Unified CLI (`wrap`, `connect`, `record`, `compile`, `run`, `verify`, `quickstart`) | [`openadapt/`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/openadapt) |
| [`openadapt-kernel`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/kernel) | Minimal `<2,800` LOC Shadow-to-Autopilot commit-boundary kernel (default-deny ledger, wire/DOM commit interceptor, stepwise live-DOM healer, cross-rail oracles, reversible pre-click probe, online ACI calibration, Ed25519 receipts, stdio MCP) | [`packages/kernel/`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/kernel) |
| [`openadapt-flow`](https://pypi.org/project/openadapt-flow/) | Full demonstration compiler, certification, qualification, governed replay, auto-repair, and BYOC connector | [`packages/flow/`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/flow) |
| [`openadapt-verify`](https://pypi.org/project/openadapt-verify/) | Standalone Python SDK (`@verified`, `checked_write`, local JIT compiler, `mcpify`) | [`packages/verify/`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/verify) |
| [`openadapt-desktop`](https://pypi.org/project/openadapt-desktop/) | OpenAdapt Desktop `0.35.0` ([download](https://openadapt.ai/download)) with Before/After step screenshot preview | [`packages/desktop/`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/desktop) |
| [`openadapt-agent`](https://pypi.org/project/openadapt-agent/) | MCP server, Agent Skills export, and A2A handoffs | [`packages/agent/`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/agent) |
| [`openadapt-types`](https://pypi.org/project/openadapt-types/) | Shared Pydantic request, result, `EvidenceScopeV1`, and receipt schemas | [`packages/types/`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/types) |
| [`openadapt-capture`](https://pypi.org/project/openadapt-capture/) | Optional local desktop recorder (`openadapt[capture]`) | [`packages/capture/`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/capture) |
| [`openadapt-privacy`](https://pypi.org/project/openadapt-privacy/) | Local PII/PHI scrubbing and redaction (`openadapt[privacy]`) | [`packages/privacy/`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/privacy) |
| [`openadapt-tray`](https://pypi.org/project/openadapt-tray/) | Menu-bar and system-tray status controls for OpenAdapt Desktop | [`packages/tray/`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/packages/tray) |

Research repositories live separately: [openadapt-evals](https://github.com/OpenAdaptAI/openadapt-evals) and [openadapt-ml](https://github.com/OpenAdaptAI/openadapt-ml).

## Surfaces and Maturity

**Web apps are the production-mature surface** (DOM structure, fresh-reload read-back, compiled API promotion, self-repair, and rollback). Desktop (Windows, macOS, Linux), RDP, and Citrix share the same record, compile, and run pipeline with narrower documented boundaries ([BACKEND_PARITY.md](packages/flow/docs/BACKEND_PARITY.md)).

| Surface | Maturity and boundaries | Signals used | Validated on |
|---|---|---|---|
| **Web apps** | Production-mature (DOM, reload read-back, API promotion, repair, rollback) | Screen + DOM + network | Gitea, Vikunja, Memos, Mealie, OpenEMR, Odoo, openIMIS; CI runs in-tree apps on every build |
| **Windows apps** | Documented boundary (UI Automation; per-app qualification required) | Screen + UI Automation | 3 of 3 saves in a WinForms test app |
| **macOS apps** | Documented boundary (Accessibility; per-app qualification required) | Screen + Accessibility | 3 of 3 saves in TextEdit; end-to-end AX repair ([#1715](https://github.com/OpenAdaptAI/OpenAdapt/pull/1715)) |
| **Linux apps (X11)** | Documented boundary (AT-SPI; X11 only) | Screen + AT-SPI | Recording and AT-SPI read in CI; backend actions unit-tested |
| **Remote desktop (RDP)** | Documented boundary (pixel-only; higher false-stop rate on visual drift) | Screen pixels | 105 runs on a real AVC444 session: 55 saves confirmed in DB, 0 wrong-record saves, 40 false stops |
| **Citrix** | Documented boundary (pixel-only; per-app qualification required) | Screen pixels | Citrix stand-in app with no DOM: 3 healthy runs, 3 stops on drift |

See [Surfaces](https://docs.openadapt.ai/explanation/surfaces/) and [Limits](https://docs.openadapt.ai/explanation/limits/) for full details.

## Record, Compile, and Replay by Hand

```bash
python -m pip install --upgrade openadapt

# Web app
openadapt record --backend web --url https://staging.your-app.example --out rec
openadapt compile rec --out bundle --name my-first-workflow
openadapt run --replay bundle --url https://staging.your-app.example --run-dir run

# Existing engine replay command for scripts
openadapt flow replay bundle --url https://staging.your-app.example --run-dir run
```

`flow replay` performs every recorded action, including real saves. Use a test or staging copy; it does not ask for approval or check the saved record. For production writes, certify, qualify, and admit the bundle, then run `openadapt run` ([Your first workflow](https://docs.openadapt.ai/tutorials/first-workflow/)).

## Run Outcomes

Every wrapped or compiled run returns a structured `transaction_outcome` so calling agents know whether to continue, escalate, or inspect the record:

| Plain meaning | `transaction_outcome` | Agent status | Next step |
|---|---|---|---|
| Done and checked | `VERIFIED` | `done` | Complete. Emits a signed Ed25519 receipt. |
| Stopped before saving | `HALTED_BEFORE_EFFECT` | `needs_review` | Fix the cause or approve a repair, then run again. |
| Check the record | `RECONCILIATION_REQUIRED` | `not_sure_if_saved` | A save may have landed. A person checks the record before retrying. |
| Finished, unchecked | `COMPLETED_UNVERIFIED` | `not_sure_if_saved` | Useful for testing; lacks independent read-back proof. |
| Saved, awaiting check | `SAVED_AWAITING_CHECK` | `not_sure_if_saved` | Settled later via `openadapt-flow verify deferred settle`. |
| Did not run | `REJECTED_POLICY`, `CANCELED`, `FAILED_PLATFORM` | `did_not_run` | Fix the policy gate or environment and run again. |

See [Run outcomes](https://docs.openadapt.ai/reference/outcomes/) for every status code.

## Security, Privacy, and Releases

- **Local-first data boundary:** Screens, typed values, and recordings stay on the machine or VPC runner executing OpenAdapt. Shareable receipts and Cloud dashboards hold counts, digests, and closed status fields.
- **Documentation:** [Security and procurement](https://docs.openadapt.ai/security/) covers data handling, deployment boundaries, evidence, subprocessors, and release integrity.
- **Earlier versions:** Pre-1.0 (`v0.46.0`) is frozen under [`legacy/`](https://github.com/OpenAdaptAI/OpenAdapt/tree/main/legacy); see [docs/LEGACY_FREEZE.md](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/docs/LEGACY_FREEZE.md) for install and migration instructions.

<details>
<summary>Release status</summary>

The block below is generated from the signed public lifecycle record.

<!-- BEGIN PRODUCTION LIFECYCLE -->
The signed ledger has seven active target admissions. Their evidence class
is `remote-safe-synthetic`, not MockMed `production_acceptance`. They remain
valid until revoked; there's no expiry date. `pip install openadapt` is the
admitted runtime (launcher 1.16.0, Flow 1.35.1). Desktop's admitted artifact
is the 0.16.0 wheel. Native Mac DMGs stay ad-hoc signed 0.15.0; don't treat
those as admitted. A Production run still needs a workflow admission for the
exact compiled bundle, application, and environment.
[Check the live signed Production record](https://docs.openadapt.ai/production-lifecycle.json).
<!-- END PRODUCTION LIFECYCLE -->

</details>

## Contributing

Launcher, kernel, engine, MCP server, desktop app, and contract changes live in this monorepo.

[Contribution guide](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/CONTRIBUTING.md) · [Issues](https://github.com/OpenAdaptAI/OpenAdapt/issues) · [Discussions](https://github.com/OpenAdaptAI/OpenAdapt/discussions) · [Report a vulnerability](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/SECURITY.md) · [Code signing policy](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/CODE_SIGNING_POLICY.md)

Maintained by [OpenAdaptAI](https://github.com/OpenAdaptAI) under the [MIT License](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/LICENSE).
