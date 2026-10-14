"""Generate the versioned public OpenAdapt Execute OpenAPI document."""

from __future__ import annotations

from typing import Any

from pydantic.json_schema import models_json_schema

from openadapt_types.execute import (
    EXECUTE_OPENAPI_SCHEMA,
    ExecuteAcceptedV1,
    ExecuteDecisionRequiredWebhookV1,
    ExecuteEvidenceReceiptV1,
    ExecuteRequestV1,
    ExecuteStateChangedWebhookV1,
    ExecuteStatusV1,
    ExecuteTerminalWebhookV1,
)


def execute_openapi_document() -> dict[str, Any]:
    """Return the portable API document; Cloud owns its implementation details."""

    _, definitions = models_json_schema(
        [
            (ExecuteRequestV1, "validation"),
            (ExecuteAcceptedV1, "validation"),
            (ExecuteStatusV1, "validation"),
            (ExecuteEvidenceReceiptV1, "validation"),
            (ExecuteStateChangedWebhookV1, "validation"),
            (ExecuteTerminalWebhookV1, "validation"),
            (ExecuteDecisionRequiredWebhookV1, "validation"),
        ],
        ref_template="#/components/schemas/{model}",
    )
    schemas = definitions["$defs"]
    execution_id = {
        "name": "execution_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"},
    }
    return {
        "openapi": "3.1.0",
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "info": {
            "title": "OpenAdapt Execute API",
            "version": "1.0.0",
            "description": (
                "Versioned async execution contract. Customer-specific connectors, "
                "evidence bytes, and Cloud implementation details are out of scope."
            ),
        },
        "x-openadapt-schema": EXECUTE_OPENAPI_SCHEMA,
        "paths": {
            "/v1/executions": {
                "post": {
                    "operationId": "createExecution",
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ExecuteRequestV1"}
                            }
                        },
                    },
                    "responses": {
                        "202": {
                            "description": "Execution accepted for durable processing.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ExecuteAcceptedV1"}
                                }
                            },
                        }
                    },
                }
            },
            "/v1/executions/{execution_id}": {
                "get": {
                    "operationId": "getExecution",
                    "security": [{"bearerAuth": []}],
                    "parameters": [execution_id],
                    "responses": {
                        "200": {
                            "description": "Current lifecycle state.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ExecuteStatusV1"}
                                }
                            },
                        }
                    },
                }
            },
            "/v1/executions/{execution_id}/receipt": {
                "get": {
                    "operationId": "getExecutionReceipt",
                    "security": [{"bearerAuth": []}],
                    "parameters": [execution_id],
                    "responses": {
                        "200": {
                            "description": "Terminal receipt with evidence identifiers.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ExecuteEvidenceReceiptV1"}
                                }
                            },
                        }
                    },
                }
            },
        },
        "webhooks": {
            "executionStateChanged": {
                "post": {
                    "security": [],
                    "description": (
                        "Verify the body HMAC with the published Execute v1 "
                        "webhook contract before accepting this delivery."
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/ExecuteStateChangedWebhookV1"
                                }
                            }
                        },
                    },
                    "responses": {"204": {"description": "Accepted by endpoint."}},
                }
            },
            "executionTerminal": {
                "post": {
                    "security": [],
                    "description": (
                        "Verify the body HMAC with the published Execute v1 "
                        "webhook contract before accepting this delivery."
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/ExecuteTerminalWebhookV1"
                                }
                            }
                        },
                    },
                    "responses": {"204": {"description": "Accepted by endpoint."}},
                }
            },
            "executionDecisionRequired": {
                "post": {
                    "security": [],
                    "description": (
                        "Verify the body HMAC with the published Execute v1 "
                        "webhook contract before accepting this delivery."
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/ExecuteDecisionRequiredWebhookV1"
                                }
                            }
                        },
                    },
                    "responses": {"204": {"description": "Accepted by endpoint."}},
                }
            },
        },
        "components": {
            "schemas": schemas,
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "OpenAdapt Execute partner token",
                    "description": (
                        "A partner-provisioned, scope-limited service token. "
                        "Do not put it in browser code or workflow parameters."
                    ),
                }
            },
        },
    }
