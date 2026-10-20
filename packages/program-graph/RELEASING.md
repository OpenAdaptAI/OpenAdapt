# Releasing @openadapt/program-graph

## Release a version

1. In a pull request, set `version` in `package.json`, run `npm install
   --package-lock-only`, and open `CHANGELOG.md` with `## vX.Y.Z (YYYY-MM-DD)`
   and its notes.
2. After it merges, run the **Release program graph** workflow
   (`.github/workflows/release-program-graph.yml`) from `main` with that
   version.

The workflow refuses any ref other than the current tip of `main`, a version
that differs from `package.json`, a changelog that does not open with that
version, and a version npm already has. It builds and tests once, packs one
tarball, publishes that tarball with `npm publish --provenance` through npm
trusted publishing (no token secret), then tags the commit
`openadapt-program-graph-vX.Y.Z` and attaches the same tarball and its
SHA256SUMS to an immutable GitHub Release.

## One-time setup

A maintainer with admin rights on the npm organization and the repository
does these once, before the first run:

1. **npm scope.** Create the `openadapt` organization on npm (or confirm it
   exists) so `@openadapt/program-graph` can be published under it.
2. **Trusted publisher.** In the package's npm settings, add a trusted
   publisher: GitHub Actions, organization `OpenAdaptAI`, repository
   `OpenAdapt`, workflow `release-program-graph.yml`, environment
   `npm-program-graph`. npm requires the package to exist before its settings
   page does; for the first version, publish from the workflow with a
   short-lived granular token held in the environment, add the trusted
   publisher, then delete the token.
3. **Environment.** Create the `npm-program-graph` environment in the
   repository with required reviewers and a deployment rule for `main` only.
4. **Tag rules.** Add `openadapt-program-graph-v*` to the tag ruleset the
   other package tags use (only the openadapt-release App creates them, and
   they cannot be moved or deleted). `scripts/verify_release_hosting.py`
   refuses to tag until it is in force.

## Moving a consumer from the vendored copy to npm

openadapt-web and openadapt-cloud carry a copy written by
`scripts/vendor.mjs`. To switch one to npm:

1. `npm install @openadapt/program-graph@X.Y.Z --save-exact`, and check the
   package's provenance with `npm audit signatures`.
2. Replace imports of the vendored `index.js` and `styles.css` with
   `@openadapt/program-graph` and `@openadapt/program-graph/styles.css`.
3. Delete the vendored directory and the CI step that ran its `check.mjs`.
