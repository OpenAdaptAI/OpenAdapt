"""Provider budget/provenance configuration reaches the trusted factory only."""

from types import SimpleNamespace

import pytest

from openadapt_flow.integrations.agents import providers, recording
from openadapt_flow.jit import keys
from openadapt_flow.wrap import cli


@pytest.mark.parametrize(
    "provider,key_provider", [("claude", "anthropic"), ("openai", "openai")]
)
def test_factory_receives_explicit_bounds_and_price_provenance(
    monkeypatch, tmp_path, provider, key_provider
):
    cache = SimpleNamespace(server=lambda *_: None)
    setup = SimpleNamespace(
        cache=cache, instruction="Save {note}", parameters=("note",)
    )
    monkeypatch.setattr(recording, "load_task", lambda *_args, **_kwargs: setup)
    observed = {}
    agent = SimpleNamespace(requires_model_key=True, model_key_provider=key_provider)

    def build(selected, **kwargs):
        observed.update(provider=selected, **kwargs)
        return agent

    loaded = []
    monkeypatch.setattr(providers, "build_agent", build)
    monkeypatch.setattr(
        keys,
        "load_customer_model_key",
        lambda selected: loaded.append(selected) or object(),
    )
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "--task",
            str(tmp_path / "task.json"),
            "--provider",
            provider,
            "--model",
            "exact-model",
            "--max-cost-usd",
            "5",
            "--price-model",
            "exact-model",
            "--pricing-source-url",
            "https://example.test/prices",
            "--pricing-verified-at",
            "2026-11-07",
            "--price",
            "input=1",
            "--price",
            "output=2",
            "--price",
            "cache_write=3",
            "--price",
            "cache_read=4",
        ]
    )
    task = cli._task(args, parser)
    assert task.agent is agent
    assert loaded == [key_provider]
    assert observed == {
        "provider": provider,
        "model": "exact-model",
        "max_turns": 40,
        "max_cost_usd": 5,
        "prices": {"input": 1, "output": 2, "cache_write": 3, "cache_read": 4},
        "max_run_seconds": 110,
        "request_timeout_seconds": 25,
        "price_model": "exact-model",
        "pricing_source_url": "https://example.test/prices",
        "pricing_verified_at": "2026-11-07",
    }


def test_wrong_adapter_key_provider_refuses_before_loading_key(monkeypatch, tmp_path):
    setup = SimpleNamespace(
        cache=SimpleNamespace(server=lambda *_: None),
        instruction="Save {note}",
        parameters=("note",),
    )
    monkeypatch.setattr(recording, "load_task", lambda *_args, **_kwargs: setup)
    monkeypatch.setattr(
        providers,
        "build_agent",
        lambda *_args, **_kwargs: SimpleNamespace(
            requires_model_key=True, model_key_provider="openai"
        ),
    )
    loaded = []
    monkeypatch.setattr(
        keys, "load_customer_model_key", lambda selected: loaded.append(selected)
    )
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--task", str(tmp_path / "task.json"), "--max-cost-usd", "5"]
    )
    with pytest.raises(SystemExit):
        cli._task(args, parser)
    assert loaded == []


def test_existing_tool_needs_no_provider_or_credential_configuration(
    monkeypatch, tmp_path
):
    cache = SimpleNamespace(server=lambda *_: object())
    setup = SimpleNamespace(
        cache=cache, instruction="Save {note}", parameters=("note",)
    )
    monkeypatch.setattr(recording, "load_task", lambda *_args, **_kwargs: setup)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("provider configuration must not run on a cache hit")

    monkeypatch.setattr(providers, "build_agent", unexpected)
    monkeypatch.setattr(keys, "load_customer_model_key", unexpected)
    parser = cli.build_parser()
    args = parser.parse_args(["--task", str(tmp_path / "task.json")])
    assert cli._task(args, parser).agent is None


def test_existing_signed_authority_and_admitter_reach_normal_task_loader(monkeypatch, tmp_path):
    from openadapt_flow.compile_agent import cli as compile_cli

    authority, admitter = object(), object()
    seen = {}
    setup = SimpleNamespace(cache=SimpleNamespace(server=lambda *_: object()), instruction="Save {note}", parameters=("note",))
    def load(path, **options):
        seen.update(path=path, **options)
        return setup
    monkeypatch.setattr(recording, "load_task", load)
    monkeypatch.setattr(compile_cli, "_teach_authority", lambda args: authority if args.teach_authorization == "signed.json" and args.approver_root == "pinned" else None)
    monkeypatch.setattr(compile_cli, "_admitter", lambda args: admitter if args.admit_key == "encrypted.pem" and args.admission_policy == "policy.json" else None)
    parser = cli.build_parser()
    args = parser.parse_args(["--task", "task.json", "--key-dir", str(tmp_path), "--teach-authorization", "signed.json", "--approver-root", "pinned", "--admit-key", "encrypted.pem", "--admission-policy", "policy.json"])
    assert cli._task(args, parser).agent is None
    assert seen == {"path": "task.json", "key_dir": str(tmp_path), "teach_authorization": authority, "admitter": admitter}


def test_admission_policy_without_key_refuses_before_task_load(monkeypatch):
    calls = []
    monkeypatch.setattr(recording, "load_task", lambda *_args, **_kwargs: calls.append(True))
    parser = cli.build_parser()
    args = parser.parse_args(["--task", "task.json", "--admission-policy", "policy.json"])
    with pytest.raises(SystemExit):
        cli._task(args, parser)
    assert calls == []
