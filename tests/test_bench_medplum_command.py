"""``openadapt bench medplum`` runs the public Medplum benchmark's runner."""

from __future__ import annotations

from click.testing import CliRunner

from openadapt import cli


def test_bench_medplum_finds_the_checkout():
    flow = cli._flow_checkout()
    assert flow is not None
    assert (flow / "benchmark" / "fhir_medplum" / "public" / "cases.yaml").is_file()


def test_bench_medplum_passes_options_to_the_runner(monkeypatch):
    seen = {}

    class Done:
        returncode = 0

    def fake_run(cmd, env):
        seen["cmd"], seen["env"] = cmd, env
        return Done()

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = CliRunner().invoke(
        cli.main, ["bench", "medplum", "--agent", "claude", "--only", "P01"]
    )
    assert result.exit_code == 0, result.output
    assert seen["cmd"][1:] == [
        "-m",
        "benchmark.fhir_medplum.public.run",
        "--agent",
        "claude",
        "--only",
        "P01",
    ]
    assert str(cli._flow_checkout()) in seen["env"]["PYTHONPATH"]
