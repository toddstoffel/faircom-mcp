# Error codes and remediation

This document defines the canonical error taxonomy used by the FairCom MCP server.

## Registry

| Code | Category | Retryable | Typical cause | Suggested remediation |
| --- | --- | --- | --- | --- |
| `validation_error` | validation | No | Invalid SQL, bad parameters, malformed input | Review the request payload and fix the invalid values. |
| `auth_error` | authentication | No | Missing or invalid credentials | Check the configured FairCom credentials or auth token. |
| `auth_denied` | authorization | No | Policy or allowlist blocked the request | Switch to a policy that includes the required tool group or adjust the allowlist. |
| `not_found` | not_found | No | Missing table, schema object, or resource | Verify the requested table or object name and inspect available metadata. |
| `rate_limit_exceeded` | resource_exhausted | Yes | Request throttling or temporary upstream limits | Wait for the retry window and slow the client request rate. |
| `conflict_error` | conflict | Yes | Lock contention or concurrent modifications | Retry after a short backoff and avoid concurrent updates. |
| `upstream_error` | upstream_failure | Yes | FairCom API timed out or returned a transient failure | Retry with backoff and inspect upstream service health. |
| `internal_error` | internal | No | Unexpected server-side failure | Retry once and contact support if it persists. |
| `not_supported` | not_supported | No | Feature not available in the current deployment | Use a supported capability or upgrade the target runtime. |
| `safety_confirmation_required` | safety | No | Write operation missing confirmation guardrails | Re-run with confirmation enabled or use a dry-run preview first. |
| `policy_violation` | authorization | No | SQL policy or tool-group policy denied the action | Adjust the policy or use a more permissive role. |

## Error payload contract

Clients should treat every error as a structured payload with the following fields:

- `code`: stable MCP error code
- `message`: human-readable summary
- `category`: high-level bucket used for routing
- `retryable`: whether a client should retry
- `hint`: remediation guidance for the caller
- `details`: context-specific data such as the statement, table, or upstream code

## Retry guidance

- Retry only for retryable categories such as `rate_limit_exceeded`, `conflict_error`, and `upstream_error`.
- Do not retry validation, authorization, or safety-gate failures.
- Prefer exponential backoff with jitter and a bounded retry count.
