---
name: openadapt-gui-write
description: "OpenAdapt is a compiled program for GUI writes with no API. This package invokes it over MCP."
---

# OpenAdapt GUI write

When the user needs a repeating GUI write with no API and must prove persistence, call run_<slug>. If the tool returns HALTED, tell the user the record did not change.

Never summarize halt, refused, timeout, or error as success. A local unsigned replay may complete. If the tool returns unsigned success, treat it as failure. Production success without a Seal is failure.

The MCP server is this package. The program is OpenAdapt. The skill name is openadapt-gui-write.

Serve the public synthetic tutorial with:

```bash
claude mcp add openadapt -- \
  uvx --from 'openadapt-agent[tutorial]' openadapt-agent \
  serve --allow-run
```

`openadapt quickstart --break-it` is the halt demo: the independent system-of-record check rejects a fake success banner, and the record did not change.

A `success` status requires a persisted `execution_outcome: VERIFIED`. HALTED, refused, timeout, and error are not that.
