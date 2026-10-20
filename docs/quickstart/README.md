# Quickstart capture and timing

- `quickstart.gif`: `openadapt quickstart` start to finish, recorded with
  [VHS](https://github.com/charmbracelet/vhs) from `quickstart.tape`. The
  browser is hidden in the recording; the output is otherwise the real command
  at real speed. Re-record with
  `PATH="$PWD/.venv/bin:$PATH" vhs docs/quickstart/quickstart.tape`.
- `inline-screenshot.png`: the third act on a terminal that draws images
  (WezTerm), with the halted run's screenshot beside the database's answer.
- `timing-2026-10-20.json`: the engine tutorial with `--break-it` (what
  `openadapt quickstart --break-it` ran before) against the new quickstart,
  three rounds each, interleaved, from `scripts/quickstart_timing.py`.

## On a real app: `--app openemr`

```bash
openadapt quickstart --app openemr
```

The same three acts on OpenEMR 8.0.0.3, a real open-source EHR, instead of the
bundled MockMed. The default stays MockMed: it needs no Docker and finishes in
about 22 seconds.

- **Needs** Docker (Docker Desktop, or Podman with `DOCKER_HOST` pointed at
  its socket) and a clone of this repository, because the OpenEMR fixture is
  repository-only:

  ```bash
  git clone https://github.com/OpenAdaptAI/OpenAdapt && cd OpenAdapt
  pip install -e . -e packages/flow
  ```

  Without either, the command says which one is missing in one line and exits
  with status 2. It never falls back to MockMed.
- **Setup.** The pinned fixture from `packages/flow/benchmark/openemr_local/`:
  pinned image digests, verified source files, a hashed SQL baseline, loopback
  ports 9741 (HTTP) and 9740 (HTTPS), and its own Compose project
  (`openadapt-quickstart-openemr`). Its state stays in
  `~/.cache/openadapt/quickstart-openemr`. The first run downloads the images
  if they're missing (about 2.6 GB unpacked), installs OpenEMR, and adds
  synthetic API clients. Later runs reuse the running copy and restore its
  baseline.
- **Act 1, show it once.** The referral as it was demonstrated once in
  OpenEMR: the recorded actions of the certified referral program, the same
  program behind the site's OpenEMR Seals
  (`benchmark/openemr_local/referral_program/`). It was compiled with the
  shared duplicate rule (`apply_referral_rules`) and qualified under
  `clinical-write`.
- **Act 2, run it.** Five synthetic referrals at once, each on its own clerk
  account and browser. Each runs through the certified run path (the Standard
  run gate, then the REST record check) and is read back from OpenEMR's
  database with SQL: "✓ checked in OpenEMR's record".
- **Act 3, break it.** The quickstart adds an existing chart (Rosa Delgado,
  born 1958-03-27), then runs a referral for a Rosa Delgado born 1958-03-22.
  OpenEMR's duplicate check lists the match, and the program stops before
  saving ("Stopped before saving: possible duplicate"). SQL finds 0 new charts.
- **Finale.** The same receipt card and Seal, plus a link to the published
  Seals at <https://openadapt.ai/seals>.

To stop the fixture, run
`docker compose -p openadapt-quickstart-openemr stop`. The next quickstart
starts it again.

The real run is covered by `test_real_openemr_quickstart` (opt in with
`OPENADAPT_REAL_QUICKSTART_OPENEMR=1` on a machine with Docker); CI skips it.
