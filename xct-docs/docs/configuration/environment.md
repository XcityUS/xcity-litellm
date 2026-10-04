# Environment variables

This reference supplements the upstream LiteLLM configuration documentation for the version shipped in this repository

## Microsoft Agent 365

The Agent 365 guardrail accepts the following optional overrides. Explicit `litellm_params` values take precedence over environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENT365_API_BASE` | `https://agent365.svc.cloud.microsoft` | Base URL for tool evaluation. Override it when using a Test or PreProd Agent 365 environment |
| `AGENT365_RESOURCE_APP_ID` | `ea9ffc3e-8a23-4a7d-836d-234d7c7565c1` | Resource application ID used for the On-Behalf-Of token exchange. Set it to the resource ID for the selected environment |

The guardrail still requires `AGENT365_TENANT_ID`, `AGENT365_CLIENT_ID`, and `AGENT365_CLIENT_SECRET`, or their corresponding explicit configuration values

## Gemini Interactions compatibility

`LITELLM_USE_LEGACY_INTERACTIONS_SCHEMA` defaults to `false`. Setting it to `true` enables the compatibility path that sends `Api-Revision: 2026-05-07` and expects the legacy `outputs` response schema instead of `steps`

Leave this flag unset for the current response schema. The compatibility path depends on the provider accepting that API revision
