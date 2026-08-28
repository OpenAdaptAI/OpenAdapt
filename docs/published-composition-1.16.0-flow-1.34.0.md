# Published launcher and Flow composition, 2026-08-28

One macOS arm64 run installed the published `openadapt==1.16.0` and
`openadapt-flow==1.34.0` wheels together. The launcher completed its local
Standard tutorial, confirmed both declared effects through the independent
system-of-record interface, and caught a backend that showed success without
saving the record.

This is package-composition evidence from one host. It is not a release
admission or a workflow admission. It doesn't establish Windows, Linux, or
customer-environment behavior.

## Environment and artifacts

- Test date: 2026-08-28
- Host: macOS 15.7.3 (24G419), arm64
- Python: 3.12.7
- Lifecycle script source: launcher tag `v1.16.0`, commit
  `089c27c046f5cd972d299361f9d68285c1896c71`
- Lifecycle script SHA-256:
  `da8dfd1c292af5d1dbb32880c9b3846c0a79aedec41e2427af9d2686eee8dcbc`
- Launcher wheel: `openadapt-1.16.0-py3-none-any.whl`
- Launcher SHA-256: `371693e7607d1cdc1ea360ef5d657c1af791a0af39677fa2ce5933e7ba712719`
- Flow wheel: `openadapt_flow-1.34.0-py3-none-any.whl`
- Flow SHA-256: `56d32818989cb3a92830080ead39e10b08718c55e00988117f22cbfeaac98854`

Both wheel hashes match the artifacts in
[`platform-manifest.json`](../platform-manifest.json) and the generated
[platform compatibility report](platform-compatibility-report.md).

## Commands

The commands below replace the run's disposable directory with
`published-composition-proof`. No repository package or source checkout was on
the test environment's import path.

```bash
git clone https://github.com/OpenAdaptAI/OpenAdapt.git published-composition-source
git -C published-composition-source checkout --detach \
  089c27c046f5cd972d299361f9d68285c1896c71
printf '%s  %s\n' \
  da8dfd1c292af5d1dbb32880c9b3846c0a79aedec41e2427af9d2686eee8dcbc \
  published-composition-source/scripts/quickstart_lifecycle.py \
  | shasum -a 256 -c -

python3.12 -m pip download --only-binary=:all: --no-deps \
  --dest published-composition-proof/wheels \
  openadapt==1.16.0 openadapt-flow==1.34.0
printf '%s  %s\n%s  %s\n' \
  371693e7607d1cdc1ea360ef5d657c1af791a0af39677fa2ce5933e7ba712719 \
  published-composition-proof/wheels/openadapt-1.16.0-py3-none-any.whl \
  56d32818989cb3a92830080ead39e10b08718c55e00988117f22cbfeaac98854 \
  published-composition-proof/wheels/openadapt_flow-1.34.0-py3-none-any.whl \
  | shasum -a 256 -c -

python3.12 published-composition-source/scripts/quickstart_lifecycle.py \
  --launcher-wheel published-composition-proof/wheels/openadapt-1.16.0-py3-none-any.whl \
  --flow-wheel published-composition-proof/wheels/openadapt_flow-1.34.0-py3-none-any.whl \
  --work-dir published-composition-proof/lifecycle \
  --source-revision published-openadapt-1.16.0-plus-flow-1.34.0
```

The lifecycle script created a new virtual environment, installed the exact
wheels with the browser and hosted extras, ran the public launcher command,
checked the generated evidence, uninstalled both packages, and confirmed that
neither package remained importable.

The fault run reinstalled only the same two wheels into that isolated
environment. It then ran:

```bash
published-composition-proof/lifecycle/venv/bin/openadapt \
  quickstart --break-it \
  --out published-composition-proof/broken-case
```

The final checks used:

```bash
published-composition-proof/lifecycle/venv/bin/openadapt version
published-composition-proof/lifecycle/venv/bin/python -m pip freeze
published-composition-proof/lifecycle/venv/bin/python -m pip check
published-composition-proof/lifecycle/venv/bin/python \
  -m pip uninstall -y openadapt openadapt-flow
```

After the last command, import probes for `openadapt` and `openadapt_flow`
returned no module. The `openadapt` and `openadapt-flow` console entry points
were also absent.

## Healthy result

The public launcher command returned these report values:

| Field | Result |
|---|---:|
| Execution outcome | `VERIFIED` |
| Transaction outcome | `VERIFIED` |
| Execution profile | `standard` |
| Authorization contracts | 1/1 passed |
| Identity contracts | 5/5 passed |
| Postcondition contracts | 9/9 passed |
| Effect contracts | 2/2 confirmed |
| Effect evidence | Tier 1, independent system of record |
| Model calls | 0 |
| Receipt | Emitted and digest-bound |

The local tutorial marked the verified transaction as billable. It did not
report or charge the local run. The clean report recorded bundle SHA-256
`9c891c874f650bde15674e401f00350e2ecd952a99bfafa04d53da2d7a49e96c`
and receipt SHA-256
`a526aecb9e1c30ac0f13931d26290b2677fce0774a35e956c6f0128be697a581`.

The launcher delegated to Flow 1.34.0. `pip check` reported no broken
requirements. Flow lint reported no error and no warning for the generated
bundle. It reported five information notices about missing pixel identifier
crops because DOM identity owned those browser steps.

## Broken result

The `--break-it` command first ran the same certified bundle against the honest
backend. It then reran that bundle against a backend that showed an on-screen
success state but rejected the write.

| Field | Result |
|---|---:|
| Execution outcome | `HALTED` |
| Transaction outcome | `RECONCILIATION_REQUIRED` |
| Transaction billable | `false` |
| Authorization contracts | 1/1 passed |
| Identity contracts | 5/5 passed |
| Postcondition contracts | 9/9 passed |
| Effect contracts | 0/2 passed |
| Retained effect evidence | One Tier 1 refutation before the halt |
| Model calls | 0 |
| Success receipt | Not emitted |

The independent read found zero matching records. Flow halted at the
consequential step and retained the refutation. The clean and broken reports
both bind workflow contract SHA-256
`955831cbb580f6f322e10d4f43a3224ea588b1f77c571a95328ebcafce2ac335`.

## Resolved environment

The isolated environment resolved these package versions:

```text
annotated-types==0.8.0
anyio==4.14.2
certifi==2026.7.22
cffi==2.1.1
click==8.5.0
cryptography==50.0.1
flatbuffers==25.12.19
greenlet==3.5.5
h11==0.16.0
httpcore==1.0.9
httpx==0.28.1
idna==3.19
jaraco.classes==3.4.0
jaraco.context==6.1.2
jaraco.functools==4.6.0
keyring==25.7.0
more-itertools==11.1.0
numpy==2.5.2
onnxruntime==1.29.0
openadapt==1.16.0
openadapt-flow==1.34.0
opencv-python==5.0.0.93
packaging==26.3
pillow==12.3.0
playwright==1.62.0
protobuf==7.36.0
pyclipper==1.4.0
pycparser==3.0
pydantic==2.13.5
pydantic_core==2.46.5
pyee==13.0.1
PyYAML==6.0.3
rapidocr-onnxruntime==1.4.4
shapely==2.1.2
six==1.17.0
tqdm==4.70.0
typing-inspection==0.4.4
typing_extensions==4.16.0
```

## Limits

This run used one macOS arm64 host and one Python version. It did not run the
Windows or Linux matrix. It did not exercise a customer application, Cloud
execution, Desktop, native capture, RDP, or Citrix. The model-call count comes
from Flow's retained report. This run did not perform an independent packet
capture. A release or workflow admission needs its own signed evidence,
validity period, revocation state, and required trial counts.
