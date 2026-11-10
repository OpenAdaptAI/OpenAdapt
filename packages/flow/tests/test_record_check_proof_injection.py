"""One source-binding model across runner, scaffold and normal CLI entrypoints.

Positive component cases stop at the first ordinary post-binding boundary;
they are not proof acceptance. The unchanged synthesis suite separately runs
both real localhost fixture proofs and their independent ground truth.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from openadapt_flow import scaffold_verifier
from openadapt_flow.__main__ import main
from openadapt_flow.record_check_proof import Fixture, ProofUnavailable, prove_live


class ReachedBoundSource(BaseException):
    """A measured post-binding boundary, never a successful proof result."""


def _source(name, seen):
    def serve():
        seen.append("serve")
        raise ReachedBoundSource

    return Fixture(
        name=name,
        serve=serve,
        reset=lambda _handle, _seed: None,
        snapshot=lambda _handle: [],
        judge=lambda _before, _after, _params: (False, "unproved"),
        scenarios=(("clean", False),),
        write_path="/synthetic",
        default_body={},
    )


@pytest.mark.parametrize("route", ["runner", "scaffold", "cli"])
@pytest.mark.parametrize(
    "kind", ["matching", "missing", "mismatched", "foreign-object"]
)
@settings(max_examples=12, deadline=None)
@given(
    name=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=24
    )
)
def test_one_explicit_source_binding_model(route, kind, name):
    seen = []
    source = _source(name, seen)
    if kind == "missing":
        source = None
    elif kind == "mismatched":
        source = replace(source, name=name + "-different")
    elif kind == "foreign-object":
        source = SimpleNamespace(**source.__dict__)

    # Fresh fixture state per generated example. No real home, authority,
    # network service, app, native window or output database is involved.
    with TemporaryDirectory(prefix="proof-source-model-") as tmp:
        root = Path(tmp)
        with pytest.MonkeyPatch.context() as patch:

            def classify(_path):
                seen.append("classify")
                raise ReachedBoundSource

            patch.setattr(scaffold_verifier, "classify_target", classify)

            def invoke():
                if route == "runner":
                    return prove_live(
                        name, {}, [], {}, work_dir=root / "proof", proof_source=source
                    )
                if route == "scaffold":
                    return scaffold_verifier.build_record_check(
                        root / "absent",
                        out_dir=root / "output",
                        prove=name,
                        proof_source=source,
                    )
                return main(
                    [
                        "scaffold-verifier",
                        str(root / "absent"),
                        "--prove=" + name,
                        "--out",
                        str(root / "output"),
                    ],
                    record_check_proof_source=source,
                )

            if kind == "matching":
                with pytest.raises(ReachedBoundSource):
                    invoke()
                assert seen == ["serve" if route == "runner" else "classify"]
            else:
                with pytest.raises(
                    (ProofUnavailable, scaffold_verifier.ScaffoldRefused),
                    match="matching caller-owned proof fixture",
                ):
                    invoke()
                assert seen == []
            assert not (root / "proof").exists()
            assert not (root / "output").exists()


def test_proof_source_is_not_a_default_for_an_unrelated_command(capsys):
    seen = []
    source = _source("arbitrary-held-out-source", seen)
    assert (
        main(["explain", "/synthetic-absent-run"], record_check_proof_source=source)
        == 2
    )
    assert seen == []
    assert "proof source requires scaffold-verifier" in capsys.readouterr().err


def test_source_without_a_requested_proof_is_not_ignored(tmp_path):
    seen = []
    with pytest.raises(
        scaffold_verifier.ScaffoldRefused, match="proof source requires --prove"
    ):
        scaffold_verifier.build_record_check(
            tmp_path / "absent", proof_source=_source("synthetic", seen)
        )
    assert seen == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("spelling", ["effect-e2e", "effect_e2e"])
@pytest.mark.parametrize(
    "route", ["runner", "cli-separated", "cli-equals", "cli-delimiter"]
)
def test_repository_aliases_bind_one_canonical_source_without_rewriting_other_values(
    spelling,
    route,
    monkeypatch,
    tmp_path,
):
    """Composition binding only; no fixture startup or proof acceptance claim."""
    from benchmark import record_check_proof as composition
    from openadapt_flow import record_check_proof as engine

    source = composition.fixture(spelling)
    assert source is composition.fixture("effect-e2e")
    assert source.name == "effect-e2e"
    seen = []
    output = tmp_path / "--prove=effect_e2e"

    def bound_runner(name, section, effects, params, *, proof_source, **kwargs):
        assert engine.require_fixture(name, proof_source) is source
        assert name == source.name
        seen.append("bound")
        raise ReachedBoundSource

    def classify(path):
        assert path == tmp_path / "absent"
        seen.append("classify")
        raise ReachedBoundSource

    normal_build = scaffold_verifier.build_record_check

    def bound_scaffold(path, out_dir=None, **kwargs):
        assert out_dir == output
        assert kwargs["prove"] == source.name
        assert kwargs["proof_source"] is source
        return normal_build(path, out_dir, **kwargs)

    monkeypatch.setattr(engine, "prove_live", bound_runner)
    monkeypatch.setattr(scaffold_verifier, "build_record_check", bound_scaffold)
    monkeypatch.setattr(scaffold_verifier, "classify_target", classify)
    with pytest.raises(ReachedBoundSource):
        if route == "runner":
            composition.prove_live(spelling, {}, [], {}, work_dir=output)
        else:
            base = ["scaffold-verifier", "--out=" + str(output)]
            if route == "cli-separated":
                argv = [*base, str(tmp_path / "absent"), "--prove", spelling]
            elif route == "cli-equals":
                argv = [*base, str(tmp_path / "absent"), "--prove=" + spelling]
            else:
                argv = [*base, "--prove", spelling, "--", str(tmp_path / "absent")]
            composition.main(argv)
    assert seen == ["bound" if route == "runner" else "classify"]
    assert not output.exists()
