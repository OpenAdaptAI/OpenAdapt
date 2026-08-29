# Distribution & discoverability — openadapt-agent

The Agent target's product state comes from its signed Production admission.
A missing, expired, revoked, mismatched, or unverifiable admission means **not
actively admitted**. This document describes how the package is made
installable and discoverable as an MCP server, the security-relevant
distinction between the *public capability* and a *user's private
bundle*, and the exact founder-owned steps to publish and list it. The
publish/submission steps are **not automated here** — they mint public,
first-party identities and are outward-facing founder actions.

## 1. The public capability vs the user's private artifact

There are two very different things people mean by "an OpenAdapt MCP
server". Keep them separate.

| | Public / official | Private / per-user |
| --- | --- | --- |
| **What it is** | the `openadapt-agent` package and its MCP server *program* | a user's compiled `openadapt-flow` workflow *bundle* |
| **What it exposes** | PHI-safe workflow and Needs Attention projections plus opt-in governed run and attended-action tools | one customer's specific recorded workflow (its steps, parameters, recorded example values) |
| **Where it lives** | PyPI + MCP registries (official, Smithery, mcp.so, Glama, PulseMCP) | the operator's own disk, passed at launch via `--bundles` |
| **Ships in the package?** | yes — code only | **never** — no bundle is embedded in the wheel, `server.json`, or any registry listing. `serve --tutorial` generates the public synthetic MockMed bundle at serve time. |

**Design consequence:** the published server takes the bundle directory
as a launch-time argument (`--bundles <dir>`) and reads nothing about a
user's workflow at build or publish time. A compiled bundle can contain
recorded example values that are business- or PHI-sensitive (see the
workspace license/PHI rules); it is the operator's artifact and stays
inside their trust boundary. The registry listing advertises the
*capability*, not any workflow.

**Read-only by default when registry-launched.** The official `server.json`
omits `--allow-run`, and the MCPB `manifest.json` leaves both execution and
attended actions off until the operator enables them in the install form. A
default install yields PHI-safe inspection and Needs Attention tools only
(`list_workflows`, `get_workflow`, `get_run_report`,
`list_needs_attention`, and `get_attention_item`). Enabling workflow runs
adds the dynamic `run_workflow_<opaque-id>` tools; enabling attended actions
adds Reject, Teach, and Escalate, while a qualified deployment config also makes
Continue and Skip available. Clients without MCP form elicitation use Flow's
attended console/CLI, where all five capabilities remain available. This
matches the security model in [`DESIGN.md`](DESIGN.md).

> There is intentionally **no** hosted, multi-tenant "official OpenAdapt
> workflow server" that exposes OpenAdapt-operated workflows to the
> public. v2 is stdio-only, single-user, local. A hosted control plane is
> a separate, proprietary surface (`openadapt-cloud`) and is out of scope
> for this package.

## 2. Canonical registry metadata

Every directory (official registry, Smithery, mcp.so, Glama, PulseMCP)
indexes the same core fields. Keep this block as the single source of
truth; the machine-readable copies are [`../server.json`](../server.json)
and [`../manifest.json`](../manifest.json).

- **Name (reverse-DNS, official registry):** `io.github.OpenAdaptAI/openadapt-agent`
- **Display name:** OpenAdapt Agent (openadapt-flow bridge)
- **PyPI package:** `openadapt-agent`
- **Version:** `2.0.2` (staged in this repository; `2.0.1` is the newest version published to PyPI)
- **Description:** OpenAdapt is a compiled program for GUI writes with no API. This package invokes it over MCP.
- **Homepage / docs:** https://docs.openadapt.ai
- **Repository:** https://github.com/OpenAdaptAI/openadapt-agent
- **License:** MIT
- **Transport:** stdio
- **Run command (uvx):** `uvx --from 'openadapt-agent[tutorial]' openadapt-agent serve --allow-run`
- **Config:** `--allow-run` with no `--bundles` (public synthetic bundle, generated at serve time), `--tutorial` (same path without implying run tools), or `--bundles` (operator's private artifact), `--runs-dir`, `--allow-attended-actions`, qualified `--config` for Continue/Skip, and optional secret `OPENADAPT_BUNDLE_KEY`
- **Tools:**
  - `list_workflows` / `get_workflow` — PHI-safe structural bundle projections with opaque IDs.
  - `get_run_report` — PHI-safe status and count summary; raw evidence stays local unless protected export was explicitly enabled.
  - `list_needs_attention` / `get_attention_item` — PHI-safe durable-pause cards and current signed-capability metadata.
  - `run_workflow_<opaque-id>` — execute through the governed `openadapt-flow run` CLI when `--allow-run`; returns `success` | `halt` | `refused` | `timeout` | `error`.
  - `continue_attention` / `skip_attention` / `reject_attention` / `teach_attention` / `escalate_attention` — exact, elicited attended decisions under Flow's capability, idempotency, verification, and audit contract.
- **Categories/tags:** mcp, agent-skills, automation, workflow, gui, governed, healthcare, rpa

## 3. Release automation vs. founder one-time actions

The build-and-publish pipeline is **automated** in
[`.github/workflows/release.yml`](../.github/workflows/release.yml). What
remains for the founder is a small set of **one-time identity/config
actions** (create accounts, enable Trusted Publishing, claim directory
listings) that mint public first-party identities and therefore stay
founder-authorized. Nothing publishes to a public index automatically
except on a deliberate version tag / GitHub Release.

### 3.0 What the workflow does (AUTOMATED)

| Trigger | Jobs that run | Publishes? |
| --- | --- | --- |
| Pull request touching release files, or `workflow_dispatch`, or any tag/release | `validate` (Python build, MCPB build, archive boundary checks, `twine check`, registry schema validation) | **No** — dry run |
| Push a `vX.Y.Z` tag, or publish a GitHub Release | `validate` -> `pypi-publish` -> `mcp-registry-publish` | **Yes** |

- **`validate`** builds the sdist+wheel and local MCPB, runs
  [`scripts/check_release_artifacts.py`](../scripts/check_release_artifacts.py)
  (fails if a wheel/sdist carries a bundle, `.enc`, run outputs, keys, or
  any non-code payload — the license/boundary gate), checks the MCPB for
  workflow/evidence payloads, runs `twine check`, validates `server.json`
  against its live schema, and runs the
  version-consistency guard (`tests/test_distribution.py`). It runs on PRs
  and manual dispatch so the pipeline is testable **without** publishing.
- **`pypi-publish`** (tag/release only) asserts the tag matches the
  package version, then uploads via **PyPI Trusted Publishing (OIDC)** —
  no long-lived token. Runs in the `pypi` GitHub environment (add required
  reviewers there if you want a human approval gate on every publish).
- **`mcp-registry-publish`** (tag/release only) waits for the new version
  to be live on PyPI (the registry validates package existence), then
  `mcp-publisher login github-oidc` + `mcp-publisher publish` — no token,
  because the repo lives under the `OpenAdaptAI` org that owns the
  `io.github.OpenAdaptAI` namespace.

To cut a release: bump the synchronized version fields (see §3.1), merge, then
`git tag vX.Y.Z && git push origin vX.Y.Z` (or publish a Release with that
tag). The first tag you push IS the first real publish — do it
deliberately.

### 3.a Required repo configuration and secrets (FOUNDER, one-time)

**In the OIDC path (recommended) there are ZERO repo secrets.** You must
do this one-time setup before the first tag:

1. **PyPI Trusted Publishing** — on https://pypi.org, keep the publisher for
   project `openadapt-agent` configured as:
   owner `OpenAdaptAI`, repo `openadapt-agent`, workflow `release.yml`,
   environment `pypi`. (PyPI account with 2FA required; the project is
   created on first OIDC upload.)
2. **GitHub environment `pypi`** — create it in repo Settings ->
   Environments. Optionally add required reviewers so each publish waits
   for a human click.
3. **MCP registry** — nothing. `io.github.OpenAdaptAI/*` is authorized by
   the repo's own GitHub OIDC identity.

**Optional token fallbacks** (only if you deliberately opt out of OIDC):

| If you set repo variable... | ...you MUST set repo secret | Used for |
| --- | --- | --- |
| `PYPI_PUBLISH_METHOD = token` | `PYPI_API_TOKEN` | PyPI upload via API token |
| `MCP_PUBLISH_METHOD = token` | `MCP_GITHUB_TOKEN` (PAT, `read:org`+`read:user`) | MCP registry `login github --token` |

The workflow **fails loud, naming the missing secret**, if a token method
is selected but its secret is absent — it never silently skips a publish.
No secret is ever committed; these live only in repo Settings -> Secrets.

### 3.1 Decide the release version

The version staged in this repository is `2.0.2`. It is not released yet: the
newest version on PyPI is `2.0.1`, the newest tag is `v2.0.1`, and there is no
`v2.0.2` tag and no GitHub Release. The commands below build the `2.0.2`
artifacts so a release can publish them. Future releases must keep the package,
module, registry, and MCPB metadata versions synchronized.

Set every future version in **four files that a CI test pins together**:
`pyproject.toml` `version`, `src/openadapt_agent/__init__.py` `__version__`,
both `version` fields in `server.json`, and `manifest.json` `version`. The
`tests/test_distribution.py` guard fails if they drift.

### 3.2 Publish to PyPI — AUTOMATED (`pypi-publish` job)

Fires automatically on a `vX.Y.Z` tag / Release once §3.a step 1-2 are
done. To reproduce locally (dry run or a manual emergency publish):

```bash
python -m build                             # sdist + wheel into dist/
python scripts/check_release_artifacts.py   # license/boundary gate
twine check dist/*
# twine upload dist/*   # emergency manual path only; prefer the tag flow
```

Verify after a release: `pip index versions openadapt-agent` shows the new
version and `uvx openadapt-agent@<version> --version` prints it.

### 3.3 Submit to the official MCP registry — AUTOMATED (`mcp-registry-publish` job)

Fires automatically after the PyPI job, using `mcp-publisher login
github-oidc` (no secret; the `OpenAdaptAI` org owns the namespace). The
job waits for the PyPI version to be installable first, because the
registry validates package existence. Local equivalent:

```bash
mcp-publisher login github-oidc     # or: login github (interactive OAuth)
mcp-publisher publish               # reads ./server.json
```

If the registry asks you to prove the PyPI package belongs to this server,
add the ownership marker it names to the package metadata and re-run.

### 3.4 List on Smithery — MCPB upload after namespace claim

Smithery's current URL form accepts public **Streamable HTTP** servers. This
repository is intentionally a local stdio server, so do not paste the GitHub
URL into the MCP Server URL field and do not add an unauthenticated HTTP shim.
Smithery's supported local path is an MCPB bundle.

Build and inspect the bundle from the exact release source:

```bash
npx -y @anthropic-ai/mcpb@2.1.2 validate manifest.json
mkdir -p mcpb-dist
npx -y @anthropic-ai/mcpb@2.1.2 pack . mcpb-dist/openadapt-agent-2.0.2.mcpb
npx -y @anthropic-ai/mcpb@2.1.2 info mcpb-dist/openadapt-agent-2.0.2.mcpb
```

The release workflow performs the same validation and creates an `mcpb`
artifact. The bundle contains the public server code and configuration only;
the operator chooses their own bundle and evidence directories during local
installation. It never contains a compiled workflow or run evidence.

One-time publication then uses Smithery's local/MCPB flow:

```bash
smithery auth login
smithery namespace use <claimed-namespace>
smithery mcp publish mcpb-dist/openadapt-agent-2.0.2.mcpb \
  -n <claimed-namespace>/openadapt-agent
```

Confirm the generated install form shows workflow bundles (required), run
evidence, governed execution (off by default), attended actions (off by
default), optional deployment config, headed mode, and the masked bundle key.
The initial namespace claim and outward-facing publication remain founder
actions; the MCPB build itself is reproducible and automated.

### 3.5 mcp.so — MANUAL (one-time submission)

- Submit at https://mcp.so (Submit / "Add MCP Server") with the repo URL.
- It scrapes the README + `server.json`; ensure the "How results come
  back" and tool table render. No account action beyond the submission
  form.

### 3.6 Glama — MOSTLY AUTOMATIC (claim to control metadata)

- Glama (https://glama.ai/mcp/servers) auto-indexes public GitHub MCP
  servers and re-scores on push. Claim the server with the GitHub org
  account to control metadata, and confirm the MIT license + repo
  metadata are picked up.

### 3.7 PulseMCP — MANUAL (one-time submission)

- Submit at https://www.pulsemcp.com (their "Submit a server" flow) with
  the repo + PyPI package. PulseMCP indexes description, install command,
  and tool list from this doc's canonical block.

### 3.8 Agent Skills directories — MANUAL (first-party listing)

The Agent Skill this package emits (`openadapt-agent emit-skill`) is a
per-bundle artifact a user generates locally; it embeds a copy of *their*
bundle, so it is **not** something to publish centrally on their behalf.
For discoverability of the *capability*:
- Add openadapt-agent to any first-party "awesome MCP / Agent Skills"
  list OpenAdapt maintains, and to the `OpenAdaptAI/OpenAdapt` README's
  integrations section.
- Do not upload user-emitted skill folders to a public skills directory:
  a skill folder contains the compiled bundle (potentially sensitive
  recorded values). Publishing one is the user's decision, per the PHI /
  artifact-egress rules.

The generic repository is therefore ready for Smithery's **MCP server**
catalog through MCPB, not for Smithery's GitHub-backed **Skill** catalog.
Creating a separate bundle-free product skill could be considered later, but
it would be a new public surface rather than a substitute for this server.

## 4. Post-publish consistency checklist

- [ ] `uvx openadapt-agent serve --bundles <dir>` starts read-only for a fresh installer.
- [ ] The exact release source validates and packs with `@anthropic-ai/mcpb@2.1.2`.
- [ ] `server.json` version == PyPI version == `__version__` (CI-pinned).
- [ ] Registry descriptions match the canonical metadata block (§2).
- [ ] README badges point at the real PyPI project once published.
- [ ] docs.openadapt.ai links back to this repo (add an "Agents & MCP" page — see the OpenAdapt docs repo; that deploy is a separate founder action).
