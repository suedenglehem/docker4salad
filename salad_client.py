"""Typed SaladCloud API client layer for starting container groups.

Implemented strictly from the local OpenAPI spec
(docs/salad/api-specs/salad-cloud.yaml, "SaladCloud API" v0.9.0-alpha.17):

  POST .../containers/{container_group_name}/start   (operationId: start_container_group)
    - required path params: organization_name, project_name, container_group_name
    - no request body
    - 202 Accepted (no response content defined); 400/403/429/default -> ProblemDetails JSON

  POST .../containers/{container_group_name}/stop    (operationId: stop_container_group)
    - same required path params, no request body
    - 202 Accepted (no response content defined); 400/403/429/default -> ProblemDetails JSON

  POST .../containers                                (operationId: create_container_group)
    - body: ContainerGroupPrototype (required: autostart_policy, container, name, replicas,
      restart_policy); priority lives INSIDE container; networking requires auth/port/protocol
    - 201 Created -> ContainerGroup JSON + Location header; 400/403/429/default -> ProblemDetails

  PATCH .../containers/{container_group_name}        (operationId: update_container_group)
    - body: ContainerGroupPatch as application/merge-patch+json
    - live behavior (observed 2026-10-01): top-level and container-level keys are
      merged (image, command, replicas, ... are kept when omitted), but
      container.environment_variables is REPLACED wholesale, not merged — to change
      a subset, send the full desired set (GET the group first)
    - 200 OK -> ContainerGroup JSON; 400/403/404/429/default -> ProblemDetails

  GET /organizations/{organization_name}/gpu-classes (operationId: list_gpu_classes)
    - 200 OK -> GpuClassesList {items: [GpuClass{id, name, prices, ...}]}

  POST /organizations/{organization_name}/projects   (UNDOCUMENTED — best effort)
    - No create-project operation exists in the spec as of v0.9.0-alpha.17; probed live
      2026-08-31 with body {"name": project_name}

  GET .../containers/{container_group_name}       (operationId: get_container_group)
    - 200 OK -> ContainerGroup JSON (used for pre/post verification only)

Auth: global ApiKeyAuth scheme -> header "Salad-Api-Key".
Server declared by the spec: https://api.salad.com/api/public

Stdlib only (urllib.request/json/dataclasses), matching api_app.py convention.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

BASE_URL = "https://api.salad.com/api/public"
API_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "salad_api.txt")

# The CDN in front of api.salad.com rejects urllib's default User-Agent
# ("Python-urllib/x.y") with 403 "error code: 1010"; an explicit UA passes.
USER_AGENT = "salad-cloud-python-client/1.0"

# ProjectName / ContainerGroupName path params (spec components/schemas):
# string, minLength 2, maxLength 63, pattern ^[a-z][a-z0-9-]{0,61}[a-z0-9]$
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")


class SaladApiError(Exception):
    """Non-success response from the SaladCloud API."""

    def __init__(self, status_code: int, problem: "ProblemDetails | None", raw_body: str) -> None:
        self.status_code = status_code
        self.problem = problem
        self.raw_body = raw_body
        detail = problem.detail if problem else None
        message = f"SaladCloud API returned HTTP {status_code}"
        if detail:
            message += f": {detail}"
        elif raw_body:
            message += f" (body: {raw_body[:200]!r})"
        super().__init__(message)


@dataclass(frozen=True)
class ProblemDetails:
    """Error body schema from the spec (components/schemas/ProblemDetails)."""

    detail: str | None = None
    instance: str | None = None
    status: int | None = None
    title: str | None = None
    type: str | None = None

    @classmethod
    def from_json(cls, payload: object) -> "ProblemDetails | None":
        if not isinstance(payload, dict):
            return None
        status = payload.get("status")
        return cls(
            detail=payload.get("detail"),
            instance=payload.get("instance"),
            status=int(status) if isinstance(status, (int, float)) else None,
            title=payload.get("title"),
            type=payload.get("type"),
        )


def _validate_group_names(organization_name: str, project_name: str, container_group_name: str) -> None:
    for name, value in (
        ("organization_name", organization_name),
        ("project_name", project_name),
        ("container_group_name", container_group_name),
    ):
        if not _NAME_RE.match(value):
            raise ValueError(
                f"{name}={value!r} violates spec pattern "
                r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$ (lowercase DNS-style name, 2-63 chars)"
            )


def _group_action_path(organization_name: str, project_name: str, container_group_name: str, action: str) -> str:
    return (
        f"/organizations/{organization_name}"
        f"/projects/{project_name}"
        f"/containers/{container_group_name}/{action}"
    )


@dataclass(frozen=True)
class StartContainerGroupRequest:
    """Typed path parameters for start_container_group (all required by the spec)."""

    organization_name: str
    project_name: str
    container_group_name: str

    def __post_init__(self) -> None:
        _validate_group_names(self.organization_name, self.project_name, self.container_group_name)

    @property
    def path(self) -> str:
        return _group_action_path(
            self.organization_name, self.project_name, self.container_group_name, "start"
        )


@dataclass(frozen=True)
class StopContainerGroupRequest:
    """Typed path parameters for stop_container_group (all required by the spec)."""

    organization_name: str
    project_name: str
    container_group_name: str

    def __post_init__(self) -> None:
        _validate_group_names(self.organization_name, self.project_name, self.container_group_name)

    @property
    def path(self) -> str:
        return _group_action_path(
            self.organization_name, self.project_name, self.container_group_name, "stop"
        )


@dataclass(frozen=True)
class DeleteContainerGroupRequest:
    """Typed path parameters for delete_container_group (all required by the spec).

    DELETE has no action suffix: the path is .../containers/{name} itself.
    """

    organization_name: str
    project_name: str
    container_group_name: str

    def __post_init__(self) -> None:
        _validate_group_names(self.organization_name, self.project_name, self.container_group_name)

    @property
    def path(self) -> str:
        return (
            f"/organizations/{self.organization_name}"
            f"/projects/{self.project_name}"
            f"/containers/{self.container_group_name}"
        )


@dataclass(frozen=True)
class StartContainerGroupResult:
    """Spec defines 202 Accepted with no response content; headers kept generically."""

    status_code: int
    reason_phrase: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StopContainerGroupResult:
    """Spec defines 202 Accepted with no response content; headers kept generically."""

    status_code: int
    reason_phrase: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ContainerGroupInfo:
    """Typed view of the GET container-group response used for verification.

    Only fields consumed by this client are surfaced; `raw` keeps the full
    ContainerGroup payload so nothing is lost.
    """

    name: str
    current_status: str | None  # pending|running|stopped|succeeded|failed|deploying
    create_time: str | None  # ISO-8601; when the group was created (its age)
    state_start_time: str | None  # ISO-8601; when the group entered its current state
    raw: dict = field(repr=False)

    @classmethod
    def from_json(cls, payload: dict) -> "ContainerGroupInfo":
        state = payload.get("current_state") or {}
        return cls(
            name=str(payload.get("name", "")),
            current_status=state.get("status"),
            create_time=payload.get("create_time"),
            state_start_time=state.get("start_time"),
            raw=payload,
        )


def load_api_key(path: str = API_KEY_FILE) -> str:
    """Read SALAD_API_KEY from salad_api.txt (the key is never printed to logs)."""
    try:
        with open(path, encoding="utf-8") as f:
            key = f.read().strip()
    except OSError as e:
        raise SaladApiError(0, None, f"cannot read API key file {path}: {e}") from e
    if not key:
        raise SaladApiError(0, None, f"API key file {path} is empty")
    return key


def _http(
    method: str,
    path: str,
    api_key: str,
    body: dict | None = None,
    timeout: float = 30.0,
    content_type: str = "application/json",
):
    url = BASE_URL + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Salad-Api-Key", api_key)
    req.add_header("User-Agent", USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.reason, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.reason, dict(e.headers), e.read()


def _parse_problem(raw: bytes) -> ProblemDetails | None:
    try:
        return ProblemDetails.from_json(json.loads(raw.decode("utf-8")))
    except (ValueError, UnicodeDecodeError):
        # api-usage.mdx notes errors may also arrive as HTML documents
        return None


def start_container_group(
    request: StartContainerGroupRequest,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> StartContainerGroupResult:
    """Start an existing container group (POST .../start).

    Returns a StartContainerGroupResult on 202 Accepted; raises SaladApiError
    for 400/403/429/default per the spec.
    """
    key = api_key if api_key is not None else load_api_key()
    status, reason, headers, raw = _http("POST", request.path, key, timeout=timeout)
    if status == 202:
        return StartContainerGroupResult(status_code=status, reason_phrase=reason, headers=headers)
    raise SaladApiError(status, _parse_problem(raw), raw.decode("utf-8", "replace"))


def stop_container_group(
    request: StopContainerGroupRequest,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> StopContainerGroupResult:
    """Stop an existing container group (POST .../stop).

    Returns a StopContainerGroupResult on 202 Accepted; raises SaladApiError
    for 400/403/429/default per the spec.
    """
    key = api_key if api_key is not None else load_api_key()
    status, reason, headers, raw = _http("POST", request.path, key, timeout=timeout)
    if status == 202:
        return StopContainerGroupResult(status_code=status, reason_phrase=reason, headers=headers)
    raise SaladApiError(status, _parse_problem(raw), raw.decode("utf-8", "replace"))


def delete_container_group(
    request: DeleteContainerGroupRequest,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> StopContainerGroupResult:
    """Delete a container group (DELETE .../containers/{name}).

    Irreversible: removes the group and all associated information. Returns
    a result on 202 Accepted; raises SaladApiError for 403/404/429/default
    per the spec. NOTE: the deleted NAME can be stuck in a name_conflict
    tombstone for 10+ minutes (observed 2026-10-01) — do not expect an
    immediate create with the same name to succeed.
    """
    key = api_key if api_key is not None else load_api_key()
    status, reason, headers, raw = _http("DELETE", request.path, key, timeout=timeout)
    if status == 202:
        return StopContainerGroupResult(status_code=status, reason_phrase=reason, headers=headers)
    raise SaladApiError(status, _parse_problem(raw), raw.decode("utf-8", "replace"))


def get_container_group(
    organization_name: str,
    project_name: str,
    container_group_name: str,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> ContainerGroupInfo:
    """Read current container-group state (GET .../containers/{name}).

    Used to verify the group before and after starting it.
    """
    key = api_key if api_key is not None else load_api_key()
    path = (
        f"/organizations/{organization_name}"
        f"/projects/{project_name}"
        f"/containers/{container_group_name}"
    )
    status, _reason, _headers, raw = _http("GET", path, key, timeout=timeout)
    if status == 200:
        return ContainerGroupInfo.from_json(json.loads(raw.decode("utf-8")))
    raise SaladApiError(status, _parse_problem(raw), raw.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# list_gpu_classes (GET /organizations/{organization_name}/gpu-classes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GpuClassInfo:
    """Typed view of a GPU class entry (spec components/schemas/GpuClass).

    Only the fields needed for gpu_classes resolution are modeled; `raw` keeps
    the full entry (prices, limits, ...) untouched.
    """

    id: str
    name: str
    raw: dict = field(default_factory=dict, repr=False)


def list_gpu_classes(
    organization_name: str,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> tuple[GpuClassInfo, ...]:
    """List the GPU classes available to an organization (operationId: list_gpu_classes).

    Returns one GpuClassInfo per item in the GpuClassesList response; raises
    SaladApiError for 404/429/default per the spec.
    """
    key = api_key if api_key is not None else load_api_key()
    path = f"/organizations/{organization_name}/gpu-classes"
    status, _reason, _headers, raw = _http("GET", path, key, timeout=timeout)
    if status == 200:
        payload = json.loads(raw.decode("utf-8"))
        items = payload.get("items") or []
        return tuple(
            GpuClassInfo(id=str(item["id"]), name=str(item["name"]), raw=item)
            for item in items
            if isinstance(item, dict) and "id" in item and "name" in item
        )
    raise SaladApiError(status, _parse_problem(raw), raw.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# create_container_group (POST .../containers)
# ---------------------------------------------------------------------------

_RESTART_POLICIES = ("always", "on_failure", "never")
_PRIORITIES = ("high", "medium", "low", "batch")
_DISPLAY_NAME_RE = re.compile(r"^[ ,-.0-9A-Za-z]+$")
_RELAXED_GROUP_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,61}[a-z0-9]$")


@dataclass(frozen=True)
class CreateContainerGroupRequest:
    """Typed ContainerGroupPrototype (spec components/schemas/ContainerGroupPrototype).

    Models the fields used by this deployment; `to_body()` emits exactly the
    spec's field names (including hyphenated keys) and omits unset optional
    fields. Note: `priority` is a container-level field in the create request
    even though GET responses expose it at the top level.
    """

    name: str
    autostart_policy: bool
    container_image: str
    environment_variables: dict[str, str]
    cpu: int
    memory_mb: int
    replicas: int
    restart_policy: str

    display_name: str | None = None
    command: tuple[str, ...] | None = None
    gpu_classes: tuple[str, ...] | None = None
    shm_size: int | None = None
    storage_amount: int | None = None
    image_caching: bool | None = None
    priority: str | None = None
    networking: dict[str, object] | None = None
    readiness_probe: dict[str, object] | None = None
    scheduled_scaling_enabled: bool | None = None
    relaxed_name: bool = False  # allow '_' fallback names beyond the spec pattern

    def __post_init__(self):
        name_re = _RELAXED_GROUP_NAME_RE if self.relaxed_name else _NAME_RE
        if not name_re.match(self.name):
            raise ValueError(
                f"container group name {self.name!r} does not match "
                r"spec pattern ^[a-z][a-z0-9-]{0,61}[a-z0-9]$"
            )
        if self.display_name is not None and not _DISPLAY_NAME_RE.match(self.display_name):
            raise ValueError(
                f"display name {self.display_name!r} does not match "
                r"spec pattern ^[ ,-.0-9A-Za-z]+$"
            )
        if not 0 <= self.replicas <= 500:
            raise ValueError(f"replicas must be 0..500, got {self.replicas}")
        if self.restart_policy not in _RESTART_POLICIES:
            raise ValueError(
                f"restart_policy must be one of {_RESTART_POLICIES}, got {self.restart_policy!r}"
            )
        if self.priority is not None and self.priority not in _PRIORITIES:
            raise ValueError(f"priority must be one of {_PRIORITIES}, got {self.priority!r}")
        if not 1 <= self.cpu <= 1024:
            raise ValueError(f"cpu must be 1..1024, got {self.cpu}")
        if not 1024 <= self.memory_mb <= 1073741824:
            raise ValueError(f"memory_mb must be 1024..1073741824, got {self.memory_mb}")
        for key, value in self.environment_variables.items():
            if not isinstance(value, str) or not 1 <= len(value) <= 1000:
                raise ValueError(
                    f"environment variable {key!r} must be a string of 1..1000 chars, "
                    f"got {value!r}"
                )
        if self.networking is not None:
            for required in ("auth", "port", "protocol"):
                if required not in self.networking:
                    raise ValueError(f"networking.{required} is required when networking is set")
            port = self.networking["port"]
            if not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError(f"networking.port must be 1..65535, got {port!r}")

    def to_body(self) -> dict:
        """Serialize to the exact ContainerGroupPrototype JSON body."""
        container: dict = {"image": self.container_image}
        if self.command is not None:
            container["command"] = list(self.command)
        if self.environment_variables:
            container["environment_variables"] = dict(self.environment_variables)
        resources: dict = {"cpu": self.cpu, "memory": self.memory_mb}
        if self.gpu_classes is not None:
            resources["gpu_classes"] = list(self.gpu_classes)
        if self.shm_size is not None:
            resources["shm_size"] = self.shm_size
        if self.storage_amount is not None:
            resources["storage_amount"] = self.storage_amount
        container["resources"] = resources
        if self.image_caching is not None:
            container["image_caching"] = self.image_caching
        if self.priority is not None:
            container["priority"] = self.priority

        body: dict = {
            "autostart_policy": self.autostart_policy,
            "container": container,
            "name": self.name,
            "replicas": self.replicas,
            "restart_policy": self.restart_policy,
        }
        if self.display_name is not None:
            body["display_name"] = self.display_name
        if self.networking is not None:
            body["networking"] = dict(self.networking)
        if self.readiness_probe is not None:
            body["readiness_probe"] = dict(self.readiness_probe)
        if self.scheduled_scaling_enabled is not None:
            body["scheduled-scaling-enabled"] = self.scheduled_scaling_enabled
        return body


@dataclass(frozen=True)
class CreateContainerGroupResult:
    """201 Created response for create_container_group (spec: ContainerGroup body)."""

    status_code: int  # always 201 on success
    reason_phrase: str
    location: str | None
    name: str
    id: str
    current_status: str | None
    raw: dict = field(repr=False)


def _header_ci(headers: dict, name: str) -> str | None:
    return next((v for k, v in headers.items() if k.lower() == name.lower()), None)


def create_container_group(
    organization_name: str,
    project_name: str,
    request: CreateContainerGroupRequest,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> CreateContainerGroupResult:
    """Create a container group (POST .../containers, operationId: create_container_group).

    Returns a CreateContainerGroupResult on 201 Created; raises SaladApiError
    for 400/403/429/default per the spec.
    """
    key = api_key if api_key is not None else load_api_key()
    path = f"/organizations/{organization_name}/projects/{project_name}/containers"
    status, reason, headers, raw = _http("POST", path, key, body=request.to_body(), timeout=timeout)
    if status == 201:
        payload = json.loads(raw.decode("utf-8"))
        state = payload.get("current_state") or {}
        return CreateContainerGroupResult(
            status_code=status,
            reason_phrase=reason,
            location=_header_ci(headers, "Location"),
            name=str(payload.get("name", "")),
            id=str(payload.get("id", "")),
            current_status=state.get("status"),
            raw=payload,
        )
    raise SaladApiError(status, _parse_problem(raw), raw.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# update_container_group (PATCH .../containers/{name})
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpdateContainerGroupRequest:
    """Typed ContainerGroupPatch fragment (spec components/schemas/ContainerGroupPatch).

    The PATCH body is served as application/merge-patch+json. Live behavior
    (observed 2026-10-01): keys present in the body are merged at the top
    level and inside `container` (image, command, replicas, ... are kept when
    omitted), but `container.environment_variables` is REPLACED wholesale
    rather than merged — to change a subset of variables, GET the group first
    and pass the full desired set here. `to_body()` omits unset fields.
    """

    environment_variables: dict[str, str] | None = None
    image: str | None = None
    # GPU class UUIDs (spec UpdateContainerResources.gpu_classes, UUID array).
    # Changing the class reschedules the group onto a worker of that class.
    gpu_classes: tuple[str, ...] | None = None
    # Spec ContainerGroupPatch.readiness_probe (ContainerGroupReadinessProbe):
    # failure_threshold 1..20, initial_delay_seconds 0..1200, period_seconds 1..120,
    # success_threshold 1..10, timeout_seconds 1..60 — all timing fields required.
    # Top-level key, merged like the other patch keys (no env interaction).
    readiness_probe: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.environment_variables is not None:
            for name, value in self.environment_variables.items():
                if not value:
                    raise ValueError(
                        f"environment variable {name!r} has an empty value; the spec "
                        "requires minLength 1 (a merge patch removes a variable via "
                        "an explicit null, not an empty string)"
                    )

    def to_body(self) -> dict:
        body: dict = {}
        if self.readiness_probe is not None:
            body["readiness_probe"] = dict(self.readiness_probe)
        container: dict = {}
        if self.image is not None:
            container["image"] = self.image
        if self.environment_variables is not None:
            container["environment_variables"] = dict(self.environment_variables)
        if self.gpu_classes is not None:
            container["resources"] = {"gpu_classes": list(self.gpu_classes)}
        if container:
            body["container"] = container
        return body


@dataclass(frozen=True)
class UpdateContainerGroupResult:
    """200 OK response for update_container_group (spec: ContainerGroup body)."""

    status_code: int  # always 200 on success
    reason_phrase: str
    name: str
    current_status: str | None
    raw: dict = field(repr=False)


def update_container_group(
    organization_name: str,
    project_name: str,
    container_group_name: str,
    request: UpdateContainerGroupRequest,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> UpdateContainerGroupResult:
    """Update a container group (PATCH .../containers/{name}, operationId: update_container_group).

    Merge-patch semantics as observed live: only the keys present in the
    request body change; fields omitted from the body (replicas, resources,
    networking, ...) are left as-is on the server. Exception:
    `container.environment_variables` is replaced wholesale rather than merged
    — pass the full desired set of variables (GET the group first). Returns an
    UpdateContainerGroupResult on 200 OK; raises SaladApiError for
    400/403/404/429/default per the spec.
    """
    _validate_group_names(organization_name, project_name, container_group_name)
    body = request.to_body()
    if not body:
        raise ValueError("nothing to update: set environment_variables, image and/or gpu_classes")
    key = api_key if api_key is not None else load_api_key()
    path = (
        f"/organizations/{organization_name}"
        f"/projects/{project_name}"
        f"/containers/{container_group_name}"
    )
    status, reason, _headers, raw = _http(
        "PATCH",
        path,
        key,
        body=body,
        content_type="application/merge-patch+json",
        timeout=timeout,
    )
    if status == 200:
        payload = json.loads(raw.decode("utf-8"))
        state = payload.get("current_state") or {}
        return UpdateContainerGroupResult(
            status_code=status,
            reason_phrase=reason,
            name=str(payload.get("name", "")),
            current_status=state.get("status"),
            raw=payload,
        )
    raise SaladApiError(status, _parse_problem(raw), raw.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# list_container_groups (GET .../containers)
# ---------------------------------------------------------------------------


def list_container_groups(
    organization_name: str,
    project_name: str,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> tuple[ContainerGroupInfo, ...]:
    """List all container groups in a project (GET .../containers).

    operationId: list_container_groups; 200 OK -> ContainerGroupCollection
    {items: [ContainerGroup]} (maxItems 100, no pagination fields in the spec).
    The items are FULL ContainerGroup objects, so each `raw` includes
    networking.dns (the gateway URL host) and current_state. Raises
    SaladApiError for 404/429/default per the spec.
    """
    key = api_key if api_key is not None else load_api_key()
    path = f"/organizations/{organization_name}/projects/{project_name}/containers"
    status, _reason, _headers, raw = _http("GET", path, key, timeout=timeout)
    if status == 200:
        payload = json.loads(raw.decode("utf-8"))
        items = payload.get("items") or []
        return tuple(
            ContainerGroupInfo.from_json(item) for item in items if isinstance(item, dict)
        )
    raise SaladApiError(status, _parse_problem(raw), raw.decode("utf-8", "replace"))


@dataclass(frozen=True)
class ContainerGroupInstanceInfo:
    """Typed view of a running container group instance (GET .../instances).

    Only fields consumed by this client are surfaced; `raw` keeps the full
    ContainerGroupInstance payload so nothing is lost. The ssh_* fields let us
    build the web-UI-style SSH line for a live instance:
    `ssh -p {ssh_port} root@{ssh_ip}` (containers run as root) — but the API
    omits them entirely when the group has no SSH config, so all three are
    optional and `ssh_line` is None in that case.
    """

    id: str
    state: str | None  # running|...
    ready: bool | None
    ssh_ip: str | None
    ssh_port: int | None
    ssh_host_key_fingerprint: str | None
    raw: dict = field(repr=False)

    @classmethod
    def from_json(cls, payload: dict) -> "ContainerGroupInstanceInfo":
        return cls(
            id=str(payload.get("id", "")),
            state=payload.get("state"),
            ready=payload.get("ready"),
            ssh_ip=payload.get("ssh_ip"),
            ssh_port=payload.get("ssh_port"),
            ssh_host_key_fingerprint=payload.get("ssh_host_key_fingerprint"),
            raw=payload,
        )

    @property
    def ssh_line(self) -> str | None:
        """The copy-paste SSH line for this instance, or None if not exposed."""
        if self.ssh_ip and self.ssh_port is not None:
            return f"ssh -p {self.ssh_port} root@{self.ssh_ip}"
        return None


def list_container_group_instances(
    organization_name: str,
    project_name: str,
    container_group_name: str,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> tuple[ContainerGroupInstanceInfo, ...]:
    """List instances of a container group (GET .../containers/{name}/instances).

    operationId: list_container_group_instances; 200 OK -> {instances:
    [ContainerGroupInstance]}. Note the payload key is `instances`, not
    `items` like the groups collection. Empty tuple when the group has no
    live instances (e.g. stopped). Raises SaladApiError for 404/429/default.
    """
    _validate_group_names(organization_name, project_name, container_group_name)
    key = api_key if api_key is not None else load_api_key()
    path = (
        f"/organizations/{organization_name}/projects/{project_name}"
        f"/containers/{container_group_name}/instances"
    )
    status, _reason, _headers, raw = _http("GET", path, key, timeout=timeout)
    if status == 200:
        payload = json.loads(raw.decode("utf-8"))
        items = payload.get("instances") or []
        return tuple(
            ContainerGroupInstanceInfo.from_json(item) for item in items if isinstance(item, dict)
        )
    raise SaladApiError(status, _parse_problem(raw), raw.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# create_project (POST /organizations/{organization_name}/projects) — UNDOCUMENTED
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreateProjectResult:
    """Response for the best-effort (undocumented) project creation call."""

    status_code: int  # 201 or 202 on success
    reason_phrase: str
    raw: dict | None = field(default=None, repr=False)


def create_project(
    organization_name: str,
    project_name: str,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> CreateProjectResult:
    """Create a project — BEST-EFFORT / UNDOCUMENTED operation.

    No create-project operation exists in salad-cloud.yaml v0.9.0-alpha.17; the
    endpoint was probed live (2026-08-31) as POST /organizations/{org}/projects
    with body {"name": project_name}. Raises SaladApiError (e.g. 404) when the
    endpoint is absent or rejects the request.
    """
    key = api_key if api_key is not None else load_api_key()
    path = f"/organizations/{organization_name}/projects"
    status, reason, _headers, raw = _http(
        "POST", path, key, body={"name": project_name}, timeout=timeout
    )
    if status in (201, 202):
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = None
        return CreateProjectResult(
            status_code=status,
            reason_phrase=reason,
            raw=payload if isinstance(payload, dict) else None,
        )
    raise SaladApiError(status, _parse_problem(raw), raw.decode("utf-8", "replace"))




