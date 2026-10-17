# openadapt-flow Agent Instructions

Read `CONTRIBUTING.md` before changing packaging, benchmarks, fixtures, release
automation, or third-party files.

## Non-negotiable package boundary

Do not ship AGPL benchmark files in a PyPI wheel or sdist. The openIMIS
reference environment and any other copied or adapted AGPL benchmark material
must remain repository-only or be obtained through a pinned, hash-verified,
opt-in upstream fetch.

Running or automating an external AGPL application does not by itself place
OpenAdapt under the AGPL. Copying or adapting its files into a distributed
artifact is a separate licensing decision.

Release validation must inspect the actual built wheel and sdist. Do not weaken
the archive exclusions or consistency tests without explicit reviewed approval
from qualified licensing counsel.
