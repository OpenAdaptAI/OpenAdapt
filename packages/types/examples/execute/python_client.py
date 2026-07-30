"""Use the published Python reference client against a partner endpoint."""

from openadapt_types import (
    EffectStrengthV1,
    ExecuteClient,
    ExecuteRequestV1,
)

client = ExecuteClient(
    base_url="https://app.openadapt.ai/api",
    bearer_token="replace-with-a-partner-provisioned-token",
)

accepted = client.create_execution(
    ExecuteRequestV1(
        qualification_id="qualification_12345678",
        workflow_version="workflow_20260729",
        workflow_digest="sha256:" + "a" * 64,
        environment_id="environment_12345678",
        parameters={"request_reference": "external-reference"},
        idempotency_key="caller_key_12345678",
        authorization_context={
            "actor_id": "caller_agent_12345678",
            "authorization_reference": "authorization_12345678",
        },
        minimum_effect_strength=EffectStrengthV1.INDEPENDENT_SYSTEM_OF_RECORD,
    )
)
print(accepted.execution_id)
