# CHANGELOG

Releases up to v2.2.0 are recorded in the [openadapt-agent repository](https://github.com/OpenAdaptAI/openadapt-agent/blob/main/CHANGELOG.md).

## v2.2.1 (2026-10-17)

First release from the [OpenAdapt monorepo](https://github.com/OpenAdaptAI/OpenAdapt) (`packages/agent`). The MCP agent server now lives in `openadapt_flow.agent`; this package is a compatibility package over it, so `openadapt-agent` and `import openadapt_agent` keep working. It requires `openadapt-flow>=2.2.0,<3`.

### Features

- The server reads the approval mode and can answer a waiting save as a delegated agent ([#1192](https://github.com/OpenAdaptAI/OpenAdapt/pull/1192)).
- After a step is repaired more than once, the server proposes a branch ([#1188](https://github.com/OpenAdaptAI/OpenAdapt/pull/1188)).
