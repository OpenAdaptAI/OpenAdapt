/**
 * Minimal TypeScript client for the public OpenAdapt Execute v1 resource paths.
 *
 * Generate complete language bindings from
 * openadapt_types/schemas/execute-v1-openapi.json when your application needs
 * all schema types. This example keeps the runtime dependency-free.
 */

export type ExecuteStatus = {
  schema_version: "openadapt.execute-status/v1";
  execution_id: string;
  state: "queued" | "running" | "decision_required" | "waiting_for_reconciliation" | "terminal";
  terminal_outcome?: "verified" | "halted_before_effect" | "reconciliation_required" | "rejected_policy" | "failed_platform" | "rolled_back_verified";
  evidence_receipt_id?: string;
  updated_at: string;
};

export type ExecuteAccepted = {
  schema_version: "openadapt.execute-accepted/v1";
  execution_id: string;
  state: "queued";
};

export class OpenAdaptExecuteClient {
  constructor(
    private readonly baseUrl: string,
    private readonly bearerToken: string,
  ) {}

  async createExecution(request: Record<string, unknown>): Promise<ExecuteAccepted> {
    return this.request("/v1/executions", { method: "POST", body: JSON.stringify(request) });
  }

  async getExecution(executionId: string): Promise<ExecuteStatus> {
    return this.request(`/v1/executions/${encodeURIComponent(executionId)}`);
  }

  async getReceipt(executionId: string): Promise<Record<string, unknown>> {
    return this.request(`/v1/executions/${encodeURIComponent(executionId)}/receipt`);
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${this.baseUrl.replace(/\/$/, "")}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${this.bearerToken}`,
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...init.headers,
      },
    });
    if (!response.ok) throw new Error(`OpenAdapt Execute request failed (${response.status})`);
    return response.json() as Promise<T>;
  }
}
