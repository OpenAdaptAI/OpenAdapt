#!/usr/bin/env bash
# Prove the Chrome extension public-key allowlist stays pinned to one exact
# value. A secret scanner that is permanently red on a known false positive is
# how a real leak gets waved through later, and an allowlist that silently
# widens is how one gets waved through quietly. This test fails if the entry
# ever becomes a rule-wide or path-wide exemption.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
config="${repo_root}/.gitleaks.toml"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

# Read the anchored value back out of the committed configuration, so this
# test can never drift from what the scanner actually allows.
python3 - "${config}" "${work}" <<'PY'
import pathlib
import re
import sys

config_path, work = sys.argv[1:3]
config = pathlib.Path(config_path).read_text(encoding="utf-8")
values = re.findall(r"'''\^(.+?)\$'''", config, re.DOTALL)
if len(values) != 1:
    raise SystemExit(f"expected exactly 1 anchored allowlist value, found {len(values)}")
allowed = values[0].replace("\\", "")
if len(allowed) != 392:
    raise SystemExit(f"the allowlisted key is {len(allowed)} characters, expected 392")

# Flip one character. Any real rotation of the extension key differs by far
# more than this, so a scanner that misses this misses every rotation.
rotated = ("B" if allowed[0] != "B" else "C") + allowed[1:]

root = pathlib.Path(work)
for name, value in (("allowed", allowed), ("rotated", rotated)):
    target = root / name / "chrome_extension"
    target.mkdir(parents=True)
    (target / "manifest.json").write_text(
        '{\n  "manifest_version": 3,\n  "name": "OpenAdapt Browser Observer",\n'
        f'  "key": "{value}"\n}}\n',
        encoding="utf-8",
    )
PY

scan() {
  # Succeeds when gitleaks reports no finding.
  gitleaks dir "$1" --config "$2" --redact --no-banner >/dev/null 2>&1
}

# 1. The exact published value must be allowlisted, or every pull request stays red.
if ! scan "${work}/allowed" "${config}"; then
  printf 'The published extension public key is not allowlisted.\n' >&2
  exit 1
fi

# 2. One changed character must still be reported. This is the narrowness property.
if scan "${work}/rotated" "${config}"; then
  printf 'The allowlist is wider than its exact value.\n' >&2
  exit 1
fi

# 3. Mutation: widening the regex must break check 2. If a widened pattern still
#    reported the rotated value, check 2 would prove nothing.
widened="${work}/widened.toml"
python3 - "${config}" "${widened}" <<'PY'
import pathlib
import re
import sys

source, destination = sys.argv[1:3]
config = pathlib.Path(source).read_text(encoding="utf-8")
mutated = re.sub(r"'''\^.+?\$'''", "'''^[A-Za-z0-9+/=]+$'''", config, count=1, flags=re.DOTALL)
if mutated == config:
    raise SystemExit("could not widen the allowlist regex")
pathlib.Path(destination).write_text(mutated, encoding="utf-8")
PY
if ! scan "${work}/rotated" "${widened}"; then
  printf 'A widened allowlist regex was not caught by this test.\n' >&2
  exit 1
fi

# 4. Mutation: a path-wide exemption must also break check 2, for the same reason.
path_exempt="${work}/path-exempt.toml"
{
  cat "${config}"
  printf '\n[[allowlists]]\ndescription = "mutation"\npaths = [ \x27\x27\x27chrome_extension/manifest.json\x27\x27\x27 ]\n'
} > "${path_exempt}"
if ! scan "${work}/rotated" "${path_exempt}"; then
  printf 'A path-wide exemption was not caught by this test.\n' >&2
  exit 1
fi

printf 'Gitleaks allowlist scope self-test passed.\n'
