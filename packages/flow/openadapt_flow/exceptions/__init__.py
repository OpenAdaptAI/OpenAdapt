"""The exception queue: any agent stops, a person answers, the run goes on
or stops before any write.

- :mod:`.registry`: the local file protocol (cards, answers, expiry, consume).
- :mod:`.detectors`: pure checks that say when to ask (no app names).
- :mod:`.mcp_server`: the ``raise_exception`` MCP tool.
- :mod:`.watch`: raise cards from a browser agent's page without the agent,
  and block its saves while one is open.
- :mod:`.cli`: ``openadapt-flow exceptions serve|watch|list|answer``.

The schemas live in :mod:`openadapt_types.exception_card`. See
``docs/EXCEPTION_QUEUE.md``.
"""
