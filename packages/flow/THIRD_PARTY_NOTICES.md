# Third-party notices

OpenAdapt Flow's original source code is licensed under the MIT License in
[`LICENSE`](LICENSE). Some benchmarks build on third-party configuration that
has a different license. That configuration is fetched from upstream at run
time, pinned by commit and SHA-256, and is not stored in this repository.

## openIMIS distribution configuration

The openIMIS reference environment (`benchmark/openimis_claims/`) uses
configuration from the openIMIS Docker distribution. These files are fetched
at run time from the exact upstream commit and checked against SHA-256 values
in `benchmark/openimis_claims/environment.lock.json`. They are not stored in
this repository:

- `compose.base.yml`, `compose.postgresql.yml`, `compose.cache.yml`
- `conf/nginx/openimis.conf`, `conf/nginx/locations/backend.loc`,
  `conf/nginx/locations/frontend.loc`, `conf/nginx/variables/var.conf`

Upstream:

- Repository: <https://github.com/openimis/openimis-dist_dkr>
- Exact commit:
  [`cd6220d1f0578e56a589c47953250c2ad3d0caa5`](https://github.com/openimis/openimis-dist_dkr/tree/cd6220d1f0578e56a589c47953250c2ad3d0caa5)
- Upstream license: GNU Affero General Public License version 3
  (`AGPL-3.0-only`)

Until 2026-10-16, adapted copies of these files were committed under
`benchmark/openimis_claims/` with their AGPL license. They were removed from
`main` and are now fetched at run time.

## OpenEMR production compose

The OpenEMR benchmark (`benchmark/openemr_local/`) derives its compose file at
run time from `docker/production/docker-compose.yml` in
<https://github.com/openemr/openemr> at commit
`7c96c8eefe460d6fadbccbe93d0fa6bf819acd69` (GPL-3.0), checked against the
SHA-256 in `benchmark/openemr_local/environment.lock.json`. The file is not
stored in this repository.

Published `openadapt-flow` wheels and source distributions exclude the
`benchmark/openimis_claims/` surface and this notice.
