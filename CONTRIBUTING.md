# Contributing to OpenAdapt

Thank you for your interest in contributing to OpenAdapt!

## Current Product Boundary

`OpenAdaptAI/OpenAdapt` is the Beta launcher/meta-package and compatibility
surface. The canonical compiler and governed runtime live in
[`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow). New engine,
replay, repair, policy, and backend work belongs there.

| Lifecycle | Repositories | Contribution scope |
|-----------|--------------|--------------------|
| **Beta product** | `OpenAdapt`, `openadapt-flow` | Launcher here; engine in `openadapt-flow` |
| **Experimental support** | `openadapt-capture`, `openadapt-privacy`, `openadapt-desktop` | Native capture, scrubbing, and authoring surfaces |
| **Agent integration** | `openadapt-agent` | MCP and Agent Skills bridge over governed Flow bundles |
| **Research** | `openadapt-ml`, `openadapt-evals`, `openadapt-grounding`, `openadapt-retrieval` | GUI-agent research and evaluation, not the product runtime |
| **Deprecated/history** | `legacy/`, pre-v2 `openadapt-agent` | Migration fixes only; no new features |

## Where to Contribute

- **This repository**: launcher packaging, unified CLI compatibility, and CI.
- **`openadapt-flow`**: compiler, replay, verification, governed repair, and
  backend implementation.
- **`openadapt-agent`**: agent-facing MCP and Agent Skills integration over
  governed Flow bundles.
- **Other repositories**: open issues only when their stated lifecycle and
  contribution guide match the proposed work.

## Getting Started

1. Fork the repository
2. Clone your fork
3. Install in development mode: `pip install -e ".[dev]"`
4. Create a branch for your changes
5. Make your changes and test locally
6. Submit a pull request

## Guidelines

- Follow existing code style
- Add tests for new functionality
- Update documentation as needed
- Keep PRs focused and small

## Licensing and the open-core boundary

OpenAdapt is open-core. The public repositories (this launcher,
`openadapt-flow`, `openadapt-capture`, `openadapt-desktop`, and the other
`openadapt-*` engine repos) are MIT licensed: the mechanism and the public
interfaces are open. The hosted control plane (OpenAdapt Cloud) is a
proprietary commercial service, and certain data and empirical tuning stay
private by policy.

What this means for contributions to the public repos:

- Do not contribute application-specific recipes, customer fixtures, or
  proprietary system identifiers derived from real deployments (for example,
  automation content tied to a specific customer's EHR configuration). CI
  runs `scripts/check_source_boundary.py` to reject that class of content.
- Do not contribute deployment-derived corpora, tuned adversary parameters,
  thresholds, oracle or connector recipes, or datasets tied to real systems
  of record. Synthetic, reproducible fixtures are welcome.
- Do not copy or vendor GPL/AGPL/SSPL or otherwise non-MIT-compatible
  material into these repositories or their built packages.
- The OpenAdapt name and logo are trademarks and are not covered by the MIT
  License; see [TRADEMARKS.md](TRADEMARKS.md).

## Developer Certificate of Origin

By submitting a contribution you certify the
[Developer Certificate of Origin (DCO) 1.1](https://developercertificate.org/):
that you wrote the contribution or otherwise have the right to submit it
under the MIT License. Signing off your commits (`git commit -s`) is
appreciated but the certification applies to every contribution regardless.

A Contributor License Agreement (CLA) is under consideration but has not
been adopted; today the DCO plus the MIT License govern contributions.

## Questions?

- [Discord](https://discord.gg/yF527cQbDG)
- [GitHub Discussions](https://github.com/OpenAdaptAI/OpenAdapt/discussions)
