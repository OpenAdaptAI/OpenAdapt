# Contributing to OpenAdapt

Thank you for your interest in contributing to OpenAdapt!

Participation in this project is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Product boundary

`OpenAdaptAI/OpenAdapt` is the launcher/meta-package and compatibility
surface. The canonical compiler and governed runtime live in
[`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow). New engine,
replay, repair, policy, and backend work belongs there.

The [signed Production record](https://docs.openadapt.ai/production-lifecycle.json)
provides the current admission state. Do not infer a lifecycle state from
package availability, repository text, or a sibling repository.

## Where to Contribute

- **This repository**: launcher packaging, unified CLI compatibility, and CI.
- **`openadapt-flow`**: compiler, replay, verification, governed repair, and
  backend implementation.
- **`openadapt-agent`**: agent-facing MCP and Agent Skills integration over
  governed Flow bundles.
- **Other repositories**: open issues only when their stated lifecycle and
  contribution guide match the proposed work.

## Getting started

1. Fork and clone the repository.
2. Create a focused branch from current `origin/main`.
3. Install the launcher in development mode:

   ```bash
   python -m pip install -e '.[dev]'
   ```

4. Run the focused test suite and formatting checks:

   ```bash
   pytest
   ruff check openadapt tests
   ```

5. Open a pull request. The pull-request template lists the required release
   and source-boundary checks.

## Guidelines

- Follow the existing code style.
- Test durable behavior and package contracts rather than ordinary copy.
- Keep changes focused and update the canonical documentation when behavior
  changes.
- Never place customer-derived data, deployment recipes, or secrets in a public
  fixture or package artifact.

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
  That script keeps no denylist of its own: it reads
  [`source-policy.public.json`](source-policy.public.json), a generated file
  rendered from OpenAdapt's canonical source-availability manifest. Do not edit
  the generated file by hand; a rule changes at its source, and the guard fails
  closed if the file is missing or incomplete.
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
