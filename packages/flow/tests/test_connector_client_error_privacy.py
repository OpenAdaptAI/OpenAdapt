"""Portable error-body privacy controls over HTTPX mock transport only."""

from __future__ import annotations

import unittest

import httpx

from openadapt_flow.connector.client import ConnectorClient, ConnectorClientError
from openadapt_flow.connector.config import ConnectorSettings


def _callback(status):
    # The smallest body the closed callback allow-list accepts.
    return {
        "run_id": "own-run",
        "org_id": "own-org",
        "workflow_id": "own-wf",
        "status": status,
    }


class BodyMustNotBeRendered(httpx.Response):
    @property
    def text(self):
        raise AssertionError("an untrusted failure body was decoded for an error")


class ClientErrorPrivacyTests(unittest.TestCase):
    def test_failure_body_is_never_rendered_for_any_endpoint_or_token_length(self):
        labels = {
            "enroll": "enroll refused",
            "poll": "poll failed",
            "ack": "ack failed",
            "run_callback": "run-callback failed",
        }
        for length in (1, 64, 299, 300, 301, 1024):
            secret = "Q" * length
            for operation, label in labels.items():
                for status in (401, 503):
                    with self.subTest(
                        length=length, operation=operation, status=status
                    ):

                        def response(_request):
                            return BodyMustNotBeRendered(
                                status,
                                json={
                                    "error": "OWN_RESPONSE_PRIVATE_MARKER " + secret,
                                },
                            )

                        with ConnectorClient(
                            "https://cp.example",
                            token=secret,
                            transport=httpx.MockTransport(response),
                        ) as client:
                            with self.assertRaises(ConnectorClientError) as failure:
                                if operation == "enroll":
                                    client.enroll(
                                        enrollment_secret=secret,
                                        org_id=None,
                                        name="own",
                                    )
                                elif operation == "poll":
                                    client.poll(1)
                                elif operation == "ack":
                                    client.ack("own-job", "failed")
                                else:
                                    client.run_callback(
                                        _callback("failed"), run_token=secret
                                    )
                        message = str(failure.exception)
                        self.assertEqual(message, f"{label}: HTTP {status}")
                        self.assertNotIn(secret[:24], message)
                        self.assertNotIn("OWN_RESPONSE_PRIVATE_MARKER", message)

    def test_long_tokens_and_success_response_contracts_are_unchanged(self):
        secret = "Q" * 1024
        self.assertEqual(ConnectorSettings(token=secret).token, secret)
        requests = []

        def response(request):
            requests.append(request.url.path)
            if request.url.path.endswith("register"):
                self.assertEqual(request.headers["x-byoc-enrollment-secret"], secret)
                return httpx.Response(200, json={"token": secret})
            if request.url.path.endswith("run-callback"):
                self.assertEqual(request.headers["x-run-token"], secret)
                return httpx.Response(299)
            self.assertEqual(request.headers["authorization"], "Bearer " + secret)
            if request.url.path.endswith("ack"):
                return httpx.Response(409, json={"already_released": True})
            if requests.count("/api/connector/poll") == 1:
                return httpx.Response(204)
            return httpx.Response(200, json={"job": {"id": "own-job"}})

        with ConnectorClient(
            "https://cp.example", transport=httpx.MockTransport(response)
        ) as client:
            self.assertEqual(
                client.enroll(enrollment_secret=secret, org_id=None, name="own"),
                {"token": secret},
            )
            self.assertEqual(client.token, secret)
            self.assertIsNone(client.poll(1))
            self.assertEqual(client.poll(1), {"id": "own-job"})
            self.assertEqual(client.ack("own-job", "done"), {"already_released": True})
            self.assertIsNone(
                client.run_callback(_callback("success"), run_token=secret)
            )
        self.assertEqual(
            requests,
            [
                "/api/connector/register",
                "/api/connector/poll",
                "/api/connector/poll",
                "/api/connector/ack",
                "/api/internal/run-callback",
            ],
        )


if __name__ == "__main__":
    unittest.main()
