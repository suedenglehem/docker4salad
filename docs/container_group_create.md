# Create Container Group — API Reference (DERIVED)

Derived from `docs/salad/api-specs/salad-cloud.yaml` (SaladCloud API v0.9.0-alpha.17,
OpenAPI 3.1.0). The spec is the source of truth; line numbers below are for quick
verification. Docs snapshot: SOURCE_COMMIT 722cfc7 (fetched 2026-08-31) — re-check
after running `./scripts/sync-salad-docs.sh`.

## Operation

| Item | Value | Spec location |
|---|---|---|
| Method + path | `POST /organizations/{organization_name}/projects/{project_name}/containers` | line 142 (`post:`) under path at line 18 |
| operationId | `create_container_group` | line 143 |
| Base URL (spec server) | `https://api.salad.com/api/public` | lines 15-16 |

## Path parameters (both required, components/parameters)

- `organization_name` — schema `OrganizationName` (line 9233)
- `project_name` — schema `ProjectName`: string, 2–63 chars, pattern `^[a-z][a-z0-9-]{0,61}[a-z0-9]$` (line 9255)

## Headers / auth

Global security (line 9349): `- ApiKeyAuth: []`; scheme (lines 5754-5758) is apiKey in header **`Salad-Api-Key`**.
Body content type: `application/json`. Read key from env (`SALAD_API_KEY`), never hard-code.

## Request body (required)

Component `#/components/requestBodies/CreateContainerGroup` (line 9287) → schema
**`ContainerGroupPrototype`** ("Container Group Creation Request", line 6464).

Top-level required: **`autostart_policy`, `container`, `name`, `replicas`, `restart_policy`** (lines 6529-6534)

| Field | Type / constraints | Required |
|---|---|---|
| autostart_policy | boolean | yes |
| container | `CreateContainer` (line 7620) | yes |
| name | string, DNS-style, 2–63, `^[a-z][a-z0-9-]{0,61}[a-z0-9]$` | yes |
| replicas | int32, 0–500 | yes |
| restart_policy | enum: `always` \| `on_failure` \| `never` (`ContainerRestartPolicy`, line 7315) | yes |
| country_codes | array of `CountryCode` (ISO 3166-1 alpha-2 enum, line 7323), minItems 1, maxItems 500; omit field to allow any country | no |
| display_name | string, 2–63, pattern `^[ ,-.0-9A-Za-z]+$` | no |
| networking | `CreateContainerGroupNetworking` (line 7666); if present **auth, port, protocol are required** (lines 7687-7690) | no |
| liveness_probe / readiness_probe / startup_probe | probe objects; actions exec/grpc/http/tcp + timing fields w/ defaults (failure_threshold 3, initial_delay_seconds 0, period_seconds 1, success_threshold 1, timeout_seconds 1) | no |
| queue_autoscaler | `ContainerGroupQueueAutoscaler`; e.g. desired_queue_length int 1–100 (line 6536) | no |
| queue_connection | `ContainerGroupQueueConnection`; if present path, port, queue_name required (lines 6615-6618) | no |
| scaling-actions | array of `ContainerGroupScalingAction`, 0–100 items | no |
| scheduled-scaling-enabled | boolean | no |

### Nested: CreateContainer (required sub-fields marked)

| Field | Type / constraints | Required |
|---|---|---|
| image | string 1–2048 (e.g. `acme/:latest`) | yes |
| resources | `CreateContainerResourceRequirements` (line 7771): **cpu** int32 1–1024 and **memory** int32 MB 1024–1073741824 required (lines 7813-7815); gpu_classes array of UUIDs (nullable), shm_size default 64, storage_amount bytes 1 GB–1 PB optional | yes |
| command | array of strings \| null, 0–100 items, each 1–1000 chars (overrides ENTRYPOINT/CMD) | no |
| environment_variables | object string→string (values 1–1000 chars) | no |
| image_caching | boolean | no |
| logging | `CreateContainerLogging`: one provider at a time — axiom / datadog / http / new_relic / splunk / tcp | no |
| priority | enum \| null: `high` \| `medium` \| `low` \| `batch` (`ContainerGroupPriority`, line 6340) | no |
| registry_authentication | `ContainerRegistryAuthentication`: aws_ecr / basic / docker_hub / gcp_gar / gcp_gcr (line 7170) | no |

### Networking enums (if networking provided)

- protocol: `ContainerNetworkingProtocol` — only value `http` (line 7152); required
- load_balancer: `ContainerGroupNetworkingLoadBalancer` — `round_robin` \| `least_number_of_connections`, default `round_robin` (line 6254)
- port: int32 1–65535; server_response_timeout default 100000 (max); single_connection_limit default false
- probe http scheme enum: `http` \| `https` (`ContainerProbeHttpScheme`, line 7160)

## Response

**201 Created** → `#/components/responses/CreateContainerGroup` (line 8973):
- Header `Location`: e.g. `/organizations/acme-corp/projects/anvil-drop-simulator/containers/sim1`
- Body: `application/json`, schema **`ContainerGroup`** (line 5810). All fields required in the response object: autostart_policy, container, country_codes, create_time, current_state (object: description, finish_time, instance_status_counts, start_time, status), display_name, id, liveness_probe, name, networking, organization_name, pending_change, priority, project_name, queue_autoscaler, queue_connection, readiness_probe, readme, replicas, restart_policy, scaling-actions, scheduled-scaling-enabled, startup_probe, update_time, version.
- OpenAPI link `get_container_group_by_name` → op `get_container_group`, param from `$response.body#/name`.

Response-side enum: `ContainerGroupStatus` = pending | running | stopped | succeeded | failed | deploying (line 6862).

## Status codes

| Code | Meaning | Body |
|---|---|---|
| 201 | Created | `ContainerGroup` + Location header |
| 400 | Bad Request | `ProblemDetails` JSON |
| 403 | Forbidden | `ProblemDetails` JSON |
| 429 | Too Many Requests | `ProblemDetails` JSON |
| default | Unknown Error (`#/components/responses/UnknownError`, line 9174) | `ProblemDetails` JSON |

`ProblemDetails` (line 8418): detail, instance (url), status (int 100–599), title, type (url, default about:blank).

## Gotchas for implementation

- Spec server includes `/api/public` prefix — full URL is `https://api.salad.com/api/public/organizations/{org}/projects/{project}/containers`.
- `country_codes`: if included, minItems is 1; omit entirely to allow any region.
- Error bodies are RFC-7807-style ProblemDetails; don't assume other shapes.
- No Salad SDK check yet — per rules, prefer an official SDK if one exists for the target language, else implement from this spec.
