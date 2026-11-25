# CHANGELOG

Releases up to v2.2.0 are recorded in the [openadapt-agent repository](https://github.com/OpenAdaptAI/openadapt-agent/blob/main/CHANGELOG.md).

## v2.2.4 (2026-11-24)

- Accept `openadapt-flow` 4.x (`openadapt-flow[vision]>=2.2.0,<5` and
  `tutorial` extra `openadapt-flow[browser,macos,vision]>=2.2.0,<5`) so
  `openadapt 1.24.0` can resolve with `openadapt-flow 4.0.0` (now live on PyPI).

## v2.2.3 (2026-11-22)

- Restore Official MCP Registry (`registry.modelcontextprotocol.io`) auto-syndication from the monorepo `release-package.yml` workflow via GitHub OIDC (`mcp-publisher`) for packages shipping `server.json`.
- Refresh `server.json` metadata (`websiteUrl` -> `https://openadapt.ai/proof` and verified local execution / read-back checks / signed receipts / zero-egress description).
- New `a2a` extra for `openadapt-agent serve --a2a`, the A2A (Agent2Agent)
  protocol adapter in openadapt-flow (docs/A2A.md). `--handoffs` adds
  `get_handoff` and `verify_handoff`. Both need an openadapt-flow that has
  them.

## v2.2.2 (2026-11-14)

- Accept `openadapt-flow` 3.x. Flow 3 moved its vision stack to the `vision`
  extra, so this package now requires `openadapt-flow[vision]>=2.2.0,<4`.

## v2.2.1 (2026-10-18)

First release from the [OpenAdapt monorepo](https://github.com/OpenAdaptAI/OpenAdapt) (`packages/agent`). The MCP agent server now lives in `openadapt_flow.agent`; this package is a compatibility package over it, so `openadapt-agent` and `import openadapt_agent` keep working. It requires `openadapt-flow>=2.2.0,<3`.

### Features

- The server reads the approval mode and can answer a waiting save as a delegated agent ([#1192](https://github.com/OpenAdaptAI/OpenAdapt/pull/1192)).
- After a step is repaired more than once, the server proposes a branch ([#1188](https://github.com/OpenAdaptAI/OpenAdapt/pull/1188)).
- The install docs use user scope and say what happens next ([#1230](https://github.com/OpenAdaptAI/OpenAdapt/pull/1230)).
