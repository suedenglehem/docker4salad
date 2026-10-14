"""Offline checks for sl_admin.py --gpu / --prio / --query / --force (no API calls).

Run: python3 -I tools/test_gpu_prio_offline.py

What it exercises:
  * card parsing: comma-separated lists, repeated flags, spaces, dedupe, rejects
  * card-name matching: the deployers' rule ('RTX 3090 (24 GB)' == 'rtx3090',
    'RTX 5090 Laptop (24 GB)' does NOT match 'rtx5090')
  * hourly prices: per-tier lookup, missing tier, the * mark (and no mark in
    --query, where there is no group)
  * GpuAvailabilityRequest: spec field names (memory, NOT memory_mb), required
    gpu_classes, UUID-only, floors omitted when unset, country-code shape
  * the availability response parse (the shape measured live 2026-10-14)
  * the priority recreate: the CREATE body copied from a group GET — image ref,
    env wholesale, resources, probe, networking WITHOUT the server-assigned dns,
    replicas/restart/autostart/scaling, and the env keys that mirror the group
    name (SALAD_GROUP = the idle watchdog's stop path) following the new name
    while MODEL_ALIAS stays put
  * _apply_cards: match -> no PATCH; diff -> PATCH with the right UUIDs; a
    parked 400 -> stop (and proceed on the old cards with --force); 'n' at the
    confirm -> NO write at all
  * _apply_priority: already-there -> no-op; typed-name mismatch -> NO delete;
    typed name -> delete + create at the new priority
  * _deploy: --gpu / --prio reach the deployer as its own flags, --no-start kept
  * main()'s flag guards (real subprocesses; they exit 2 before any API call)

Nothing here touches the network: the client functions are monkeypatched on the
imported module, the deployer subprocess is faked, and the guard runs exit at
the usage checks.
"""
import importlib.util
import json
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    """Import utils/sl_admin.py as a module (it is not a package)."""
    path = os.path.join(_ROOT, "utils", "sl_admin.py")
    spec = importlib.util.spec_from_file_location("sl_admin", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ADMIN = _load()
sc = ADMIN.sc
ORG, PROJ = "ma-casa-in-paris", "llm"
fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        fails.append(f"{name}  [{detail}]")
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + (f"  ({detail})" if detail and not cond else ""))


# --- canned API shapes (measured live 2026-10-14) ----------------------------

GPU_CLASSES = [
    {"id": "a5db5c50-cbcb-4596-ae80-6a0c8090d80f", "name": "RTX 3090 (24 GB)",
     "prices": [{"price": "0.170", "priority": "high"}, {"price": "0.143", "priority": "medium"},
                {"price": "0.117", "priority": "low"}, {"price": "0.09", "priority": "batch"}]},
    {"id": "ed563892-aacd-40f5-80b7-90c9be6c759b", "name": "RTX 4090 (24 GB)",
     "prices": [{"price": "0.330", "priority": "high"}, {"price": "0.273", "priority": "medium"},
                {"price": "0.217", "priority": "low"}, {"price": "0.160", "priority": "batch"}]},
    {"id": "851399fb-7329-4195-a042-d6514b28cf33", "name": "RTX 5090 (32 GB)",
     "prices": [{"price": "0.500", "priority": "high"}]},
    {"id": "43a49c0c-f860-40e9-a509-702d0dba0902", "name": "RTX 3080 (10 GB)", "prices": []},
]

GROUP = {
    "name": "qwen38-27b-q5", "display_name": "qwen38-27b-q5", "id": "g-1", "version": 7,
    "priority": "low", "replicas": 1, "restart_policy": "always", "autostart_policy": False,
    "scheduled-scaling-enabled": True, "pending_change": False,
    "container": {
        "hash": "h", "image_caching": True, "size": 1,
        "image": "boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision@sha256:75c8cf66",
        "command": ["bash", "-c", "run.sh"],
        "environment_variables": {
            "NAME": "qwen38-27b-q5", "SALAD_GROUP": "qwen38-27b-q5",
            "SALAD_ORG": ORG, "SALAD_PROJECT": PROJ,
            "MODEL_ALIAS": "qwen38-27b", "CTX_SIZE": "90000",
            "HF_TOKEN": "hf_secret", "SALAD_STOP_KEY": "YnM6a2V5"},
        "resources": {"cpu": 8, "memory": 16384, "shm_size": 64,
                      "storage_amount": 53687091200,
                      "gpu_classes": ["a5db5c50-cbcb-4596-ae80-6a0c8090d80f"]},
    },
    "networking": {"dns": "pumpkin-endive-x.salad.cloud", "port": 8888, "protocol": "http",
                   "auth": True, "load_balancer": "round_robin",
                   "single_connection_limit": False, "client_request_timeout": 100000,
                   "server_response_timeout": 100000},
    "readiness_probe": {"initial_delay_seconds": 30, "period_seconds": 120, "timeout_seconds": 1,
                        "success_threshold": 1, "failure_threshold": 20,
                        "http": {"path": "/ready", "port": 8889, "scheme": "http", "headers": []}},
    "current_state": {"status": "stopped"},
}

PARKED = {"type": "https://api.salad.com/api/public/errors/pending_update_in_progress",
          "title": "Pending update in progress", "status": 400}


class FakeGroup:
    """Minimal stand-in for ContainerGroupInfo (name/status/raw)."""

    def __init__(self, raw: dict):
        self.raw = raw
        self.name = raw["name"]
        self.current_status = (raw.get("current_state") or {}).get("status")


def install_fake_api(state: dict) -> list:
    """Monkeypatch the client calls ADMIN uses; record every call in `calls`.

    `state`: {"group": <GET body> | None, "patch_lands": bool, "patch_error": dict|None}.
    A GET answers only for the group's own name (404 otherwise) — which is what
    lets `_fresh_name` find a free name, and makes the post-deploy status GET 404.
    """
    calls: list = []

    def list_gpu_classes(org, api_key=None):
        calls.append(("list_gpu_classes", org))
        return tuple(sc.GpuClassInfo(id=c["id"], name=c["name"], raw=c) for c in GPU_CLASSES)

    def get_container_group(org, project, name, api_key=None):
        calls.append(("get", name))
        raw = state.get("group")
        if raw is None or raw["name"] != name:
            raise sc.SaladApiError(404, None, "{}")
        return FakeGroup(raw)

    def update_container_group(org, project, name, request, api_key=None):
        calls.append(("patch", name, request.to_body()))
        if state.get("patch_error"):
            raise sc.SaladApiError(400, None, json.dumps(state["patch_error"]))
        if state.get("patch_lands"):
            state["group"]["container"]["resources"]["gpu_classes"] = list(request.gpu_classes)
        return None

    def delete_container_group(request, api_key=None):
        calls.append(("delete", request.container_group_name))
        state["group"] = None
        return None

    def create_container_group(org, project, request, api_key=None):
        calls.append(("create", request.name, request.to_body()))
        body = request.to_body()
        state["group"] = {"name": request.name, "current_state": {"status": "stopped"},
                          "pending_change": False, "priority": body["container"].get("priority"),
                          "container": body["container"]}
        return sc.CreateContainerGroupResult(201, "Created", None, request.name, "id",
                                             "stopped", body)

    sc.list_gpu_classes = list_gpu_classes
    sc.get_container_group = get_container_group
    sc.update_container_group = update_container_group
    sc.delete_container_group = delete_container_group
    sc.create_container_group = create_container_group
    return calls


def answer(*replies):
    """Feed canned stdin to input() (a module-level name shadows the builtin)."""
    it = iter(replies)

    def _input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    ADMIN.input = _input


def fresh_group() -> dict:
    return json.loads(json.dumps(GROUP))


def main() -> int:
    # --- 1. card parsing -----------------------------------------------------
    check("parse: comma list", ADMIN._parse_cards("rtx3090,rtx4090") == ["rtx3090", "rtx4090"])
    check("parse: spaces + repeated flags",
          ADMIN._parse_cards([" rtx3090 ", "rtx4090, rtx3080ti"]) == ["rtx3090", "rtx4090", "rtx3080ti"])
    check("parse: dedupe keeps order",
          ADMIN._parse_cards(["rtx4090", "rtx3090,rtx4090"]) == ["rtx4090", "rtx3090"])
    check("parse: empty tokens skipped", ADMIN._parse_cards("rtx3090,,") == ["rtx3090"])
    for bad in ("rtx2080", "", ",,"):
        try:
            ADMIN._parse_cards(bad)
            check(f"parse: rejects {bad!r}", False, "no ValueError")
        except ValueError as e:
            check(f"parse: rejects {bad!r}", "valid" in str(e), str(e)[:40])
    check("parse: all seven cards accepted",
          ADMIN._parse_cards(",".join(ADMIN.CARD_CHOICES)) == list(ADMIN.CARD_CHOICES))

    # --- 2. card-name matching (the deployers' rule) -------------------------
    check("norm: 'RTX 3090 (24 GB)' -> rtx3090", ADMIN._norm_card("RTX 3090 (24 GB)") == "rtx3090")
    check("norm: 'RTX 5090 Laptop (24 GB)' does NOT match rtx5090",
          ADMIN._norm_card("RTX 5090 Laptop (24 GB)") != "rtx5090")
    install_fake_api({"group": fresh_group()})
    check("resolve: names -> the org's classes",
          [c.id for c in ADMIN._resolve_cards(ORG, ["rtx3090", "rtx4090"], "k")]
          == ["a5db5c50-cbcb-4596-ae80-6a0c8090d80f", "ed563892-aacd-40f5-80b7-90c9be6c759b"])
    try:
        ADMIN._resolve_cards(ORG, ["rtx4080"], "k")
        check("resolve: a card the org does not offer raises", False, "no ValueError")
    except ValueError as e:
        check("resolve: a card the org does not offer raises", "not offered" in str(e), str(e)[:60])

    # --- 3. prices ------------------------------------------------------------
    cls3090 = sc.GpuClassInfo(id="x", name="RTX 3090 (24 GB)", raw=GPU_CLASSES[0])
    cls5090 = sc.GpuClassInfo(id="y", name="RTX 5090 (32 GB)", raw=GPU_CLASSES[2])
    cls3080 = sc.GpuClassInfo(id="z", name="RTX 3080 (10 GB)", raw=GPU_CLASSES[3])
    check("price: per tier", [ADMIN._card_price(cls3090, t) for t in ADMIN.PRICE_TIERS]
          == [0.170, 0.143, 0.117, 0.09])
    check("price: missing tier -> None", ADMIN._card_price(cls5090, "low") is None)
    check("price: no prices at all -> None", ADMIN._card_price(cls3080, "high") is None)
    row = ADMIN._card_row(cls3090, "low")
    check("row: marks the group's own tier with *", "L0.117*" in row and "H0.170 " in row, row)
    check("row: no mark when prio is None (--query)", "*" not in ADMIN._card_row(cls3090, None))
    check("row: missing price prints -", "L-" in ADMIN._card_row(cls5090, None),
          ADMIN._card_row(cls5090, None))

    # --- 4. availability request / response ----------------------------------
    req = sc.GpuAvailabilityRequest(
        gpu_classes=("a5db5c50-cbcb-4596-ae80-6a0c8090d80f",),
        cpu=8, memory_mb=16384, storage_amount=53687091200)
    body = req.to_body()
    check("avail body: the spec key is 'memory' (not memory_mb)",
          body.get("memory") == 16384 and "memory_mb" not in body, json.dumps(body))
    check("avail body: floors omitted when unset",
          sc.GpuAvailabilityRequest(gpu_classes=("a5db5c50-cbcb-4596-ae80-6a0c8090d80f",)).to_body()
          == {"gpu_classes": ["a5db5c50-cbcb-4596-ae80-6a0c8090d80f"]})
    for bad in ({"gpu_classes": ()}, {"gpu_classes": ("rtx3090",)},
                {"gpu_classes": ("a5db5c50-cbcb-4596-ae80-6a0c8090d80f",), "cpu": 0},
                {"gpu_classes": ("a5db5c50-cbcb-4596-ae80-6a0c8090d80f",), "country_codes": ("USA",)}):
        try:
            sc.GpuAvailabilityRequest(**bad)
            check(f"avail request rejects {sorted(bad)}", False, "no ValueError")
        except ValueError:
            check(f"avail request rejects {sorted(bad)}", True)
    av = sc.GpuAvailabilityInfo(
        available_gpu_batch=1, available_gpu_high=287, available_gpu_low=12,
        available_gpu_medium=34, on_call_gpu=0,
        raw={"available_gpu_batch": 1, "available_gpu_high": 287, "available_gpu_low": 12,
             "available_gpu_medium": 34, "on_call_gpu": 0})
    check("avail response: tiers parsed",
          (av.available_gpu_high, av.available_gpu_medium, av.available_gpu_low,
           av.available_gpu_batch, av.on_call_gpu) == (287, 34, 12, 1, 0))

    # --- 5. the priority recreate: CREATE body copied from a GET -------------
    state = {"group": fresh_group()}
    install_fake_api(state)
    spec = ADMIN._spec_from_raw(state["group"], "qwen38-27b-q5-high", "high")
    cb = spec.to_body()
    check("recreate: priority is the requested one", cb["container"]["priority"] == "high")
    check("recreate: image ref copied verbatim (digest-pinned)",
          cb["container"]["image"] == GROUP["container"]["image"])
    check("recreate: env copied wholesale",
          set(cb["container"]["environment_variables"]) == set(GROUP["container"]["environment_variables"]))
    check("recreate: SALAD_GROUP follows the new name (watchdog stop path)",
          spec.environment_variables["SALAD_GROUP"] == "qwen38-27b-q5-high")
    check("recreate: NAME label follows too",
          spec.environment_variables["NAME"] == "qwen38-27b-q5-high")
    check("recreate: MODEL_ALIAS stays stable (clients depend on it)",
          spec.environment_variables["MODEL_ALIAS"] == "qwen38-27b")
    check("recreate: org/project env untouched",
          spec.environment_variables["SALAD_ORG"] == ORG
          and spec.environment_variables["SALAD_PROJECT"] == PROJ)
    check("recreate: resources copied", cb["container"]["resources"] == GROUP["container"]["resources"])
    check("recreate: card list copied",
          cb["container"]["resources"]["gpu_classes"] == GROUP["container"]["resources"]["gpu_classes"])
    check("recreate: networking has NO server-assigned dns",
          "dns" not in (cb.get("networking") or {})
          and cb["networking"]["port"] == 8888 and cb["networking"]["auth"] is True)
    check("recreate: readiness probe copied", cb["readiness_probe"] == GROUP["readiness_probe"])
    check("recreate: command / replicas / restart / autostart / scaling copied",
          cb["container"]["command"] == ["bash", "-c", "run.sh"]
          and cb["replicas"] == 1 and cb["restart_policy"] == "always"
          and cb["autostart_policy"] is False
          and cb["scheduled-scaling-enabled"] is True)
    check("recreate: name is valid for Salad", bool(ADMIN.SALAD_NAME_RE.match(spec.name)), spec.name)

    # --- 6. _fresh_name picks a free name (404 = free) -----------------------
    state = {"group": fresh_group()}
    calls = install_fake_api(state)
    check("fresh name: '<base>-<prio>' when free",
          ADMIN._fresh_name("qwen38-27b-q5", "high", ORG, PROJ, "k") == "qwen38-27b-q5-high")

    def get_taken(org, project, name, api_key=None):
        calls.append(("probe", name))
        if name == "qwen38-27b-q5-high":        # taken
            return FakeGroup({"name": name, "current_state": {"status": "stopped"}})
        raise sc.SaladApiError(404, None, "{}")

    sc.get_container_group = get_taken
    check("fresh name: skips a taken '<base>-<prio>'",
          ADMIN._fresh_name("qwen38-27b-q5", "high", ORG, PROJ, "k") == "qwen38-27b-q5-high-2")
    long_name = ADMIN._fresh_name("q" * 60, "medium", ORG, PROJ, "k")
    check("fresh name: the 63-char cap stays a valid name",
          len(long_name) <= 63 and bool(ADMIN.SALAD_NAME_RE.match(long_name)), long_name)

    # --- 7. _apply_cards -----------------------------------------------------
    # (a) cards already match -> no PATCH, proceed
    state = {"group": fresh_group()}
    calls = install_fake_api(state)
    rc, out = ADMIN._apply_cards(ORG, PROJ, FakeGroup(state["group"]), ["rtx3090"], "k", force=False)
    check("cards: match -> no PATCH, proceed", rc == 0 and not any(c[0] == "patch" for c in calls))

    # (b) diff + 'n' at the confirm -> NO write
    state = {"group": fresh_group()}
    calls = install_fake_api(state)
    answer("n")
    rc, out = ADMIN._apply_cards(ORG, PROJ, FakeGroup(state["group"]), ["rtx4090"], "k", force=False)
    check("cards: diff + 'n' -> abort, no PATCH", rc == 1 and not any(c[0] == "patch" for c in calls))

    # (c) diff + --force -> PATCH the requested UUIDs, lands, proceed
    state = {"group": fresh_group(), "patch_lands": True}
    calls = install_fake_api(state)
    rc, out = ADMIN._apply_cards(ORG, PROJ, FakeGroup(state["group"]), ["rtx4090"], "k", force=True)
    patches = [c for c in calls if c[0] == "patch"]
    check("cards: --force -> PATCH the requested card",
          rc == 0 and len(patches) == 1
          and patches[0][2]["container"]["resources"]["gpu_classes"]
          == ["ed563892-aacd-40f5-80b7-90c9be6c759b"])
    check("cards: a landed PATCH is verified from the readback",
          rc == 0 and out is not None
          and set(ADMIN._group_cards(out.raw)) == {"ed563892-aacd-40f5-80b7-90c9be6c759b"})

    # (d) comma list -> every card in ONE PATCH
    state = {"group": fresh_group(), "patch_lands": True}
    calls = install_fake_api(state)
    rc, out = ADMIN._apply_cards(ORG, PROJ, FakeGroup(state["group"]), ["rtx4090", "rtx3090"], "k", True)
    check("cards: comma list PATCHes every card",
          rc == 0 and sorted([c for c in calls if c[0] == "patch"][0][2]["container"]["resources"]["gpu_classes"])
          == sorted(["ed563892-aacd-40f5-80b7-90c9be6c759b", "a5db5c50-cbcb-4596-ae80-6a0c8090d80f"]))

    # (e) parked group (400 pending_update_in_progress): stop without force,
    #     proceed on the CURRENT cards with force
    state = {"group": fresh_group(), "patch_error": PARKED}
    calls = install_fake_api(state)
    rc, out = ADMIN._apply_cards(ORG, PROJ, FakeGroup(state["group"]), ["rtx4090"], "k", force=False)
    check("cards: parked PATCH -> stop (no start on the old card)", rc == 1)
    state = {"group": fresh_group(), "patch_error": PARKED}
    rc, out = ADMIN._apply_cards(ORG, PROJ, FakeGroup(state["group"]), ["rtx4090"], "k", force=True)
    check("cards: parked + --force -> proceed on the CURRENT cards",
          rc == 0 and set(ADMIN._group_cards(out.raw)) == {"a5db5c50-cbcb-4596-ae80-6a0c8090d80f"})

    # (f) PATCH accepted, readback lags -> proceed (the parked spec lands at this start)
    state = {"group": fresh_group(), "patch_lands": False}
    rc, out = ADMIN._apply_cards(ORG, PROJ, FakeGroup(state["group"]), ["rtx4090"], "k", True)
    check("cards: lagging readback -> proceed", rc == 0)

    # (g) a card the org does not offer -> stop before any write
    state = {"group": fresh_group()}
    calls = install_fake_api(state)
    rc, out = ADMIN._apply_cards(ORG, PROJ, FakeGroup(state["group"]), ["rtx4080"], "k", True)
    check("cards: unofferable card -> stop, no PATCH",
          rc == 1 and not any(c[0] == "patch" for c in calls))

    # --- 8. _apply_priority --------------------------------------------------
    # (a) already at that priority -> no-op, no delete
    state = {"group": fresh_group()}
    calls = install_fake_api(state)
    rc, new = ADMIN._apply_priority(ORG, PROJ, FakeGroup(state["group"]), "low", "k", force=True)
    check("prio: already there -> no-op, keep the group",
          rc == 0 and new is None and not any(c[0] == "delete" for c in calls))

    # (b) typed name mismatch -> NO delete, NO create
    state = {"group": fresh_group()}
    calls = install_fake_api(state)
    answer("wrong-name")
    rc, new = ADMIN._apply_priority(ORG, PROJ, FakeGroup(state["group"]), "high", "k", force=False)
    check("prio: typed-name mismatch -> abort, nothing deleted",
          rc == 1 and not any(c[0] in ("delete", "create") for c in calls))

    # (c) --force -> delete + create at the new priority, env rewritten
    state = {"group": fresh_group()}
    calls = install_fake_api(state)
    rc, new = ADMIN._apply_priority(ORG, PROJ, FakeGroup(state["group"]), "high", "k", force=True)
    creates = [c for c in calls if c[0] == "create"]
    check("prio: --force -> delete then create at the new priority",
          rc == 0 and new == "qwen38-27b-q5-high"
          and [c[0] for c in calls if c[0] in ("delete", "create")] == ["delete", "create"]
          and creates[0][2]["container"]["priority"] == "high")
    check("prio: recreated env points at the new group",
          creates[0][2]["container"]["environment_variables"]["SALAD_GROUP"] == "qwen38-27b-q5-high"
          and creates[0][2]["container"]["environment_variables"]["MODEL_ALIAS"] == "qwen38-27b")
    check("prio: recreated group keeps the card + image + probe",
          creates[0][2]["container"]["resources"]["gpu_classes"]
          == ["a5db5c50-cbcb-4596-ae80-6a0c8090d80f"]
          and creates[0][2]["container"]["image"] == GROUP["container"]["image"]
          and creates[0][2]["readiness_probe"] == GROUP["readiness_probe"])
    check("prio: recreated group keeps autostart as it was (False = no billing on CREATE)",
          creates[0][2]["autostart_policy"] is False)

    # (d) name_conflict on create (the tombstone) -> stop, and say what is gone
    state = {"group": fresh_group()}
    calls = install_fake_api(state)

    def create_conflict(org, project, request, api_key=None):
        calls.append(("create", request.name, request.to_body()))
        raise sc.SaladApiError(400, None, json.dumps({"status": 400, "title": "name_conflict"}))

    sc.create_container_group = create_conflict
    rc, new = ADMIN._apply_priority(ORG, PROJ, FakeGroup(state["group"]), "high", "k", force=True)
    check("prio: tombstoned name -> stop after the delete (honest report)",
          rc == 1 and new is None and any(c[0] == "delete" for c in calls))

    # --- 9. _deploy: the flags reach the deployer ----------------------------
    cmds: list = []

    class FakeSubprocess:
        def call(self, cmd):
            cmds.append(cmd)
            return 0

    real_subprocess = ADMIN.subprocess
    ADMIN.subprocess = FakeSubprocess()
    install_fake_api({"group": None})          # the post-deploy status GET 404s -> handled
    rc = ADMIN._deploy("k", "q6", ORG, PROJ, "probe-card-prio", start=False, extra=[],
                       cards=["rtx3090", "rtx4090"], prio="medium", force=False)
    cmd = cmds[-1] if cmds else []
    check("deploy: rc 0 with the deployer faked", rc == 0 and bool(cmd), str(cmd))
    check("deploy: --gpu list -> the deployer's own --gpu flags",
          cmd[cmd.index("--gpu") + 1:cmd.index("--gpu") + 3] == ["rtx3090", "rtx4090"], " ".join(cmd))
    check("deploy: --prio med -> --priority medium on the deployer line",
          cmd[cmd.index("--priority") + 1] == "medium", " ".join(cmd))
    check("deploy: free by default (--no-start kept)", "--no-start" in cmd, " ".join(cmd))
    check("deploy: 'med' never reaches the deployer", "med" not in cmd)
    ADMIN.subprocess = real_subprocess

    # --- 10. main() guards (real subprocesses; exit before any API call) -----
    def run(*args) -> tuple[int, str]:
        p = subprocess.run([sys.executable, os.path.join("utils", "sl_admin.py"), *args],
                           capture_output=True, text=True, cwd=_ROOT, timeout=90)
        return p.returncode, p.stdout + p.stderr

    cases = [
        (["--query"], "--query needs --gpu"),
        (["start", "--query"], "--query needs --gpu"),
        (["list", "--gpu", "rtx3090"], "--gpu applies to"),
        (["stop", "--prio", "high"], "--prio applies to"),
        (["stop", "--force"], "--y/--force applies to"),
        (["start", "--gpu", "rtx2080"], "unknown card"),
        (["start", "--gpu"], "--gpu needs a value"),
        (["start", "--prio", "ultra"], "--prio must be one of"),
        (["start", "--prio"], "--prio needs a value"),
        (["list", "--group", "x"], "--group applies to"),
    ]
    for args, needle in cases:
        rc, out = run(*args)
        check(f"guard: {' '.join(args)} -> {needle!r}", rc == 2 and needle in out,
              f"rc={rc} {out.strip()[:90]}")

    print(f"\n{len(fails)} failure(s)" if fails else "\nALL GPU/PRIORITY CHECKS PASSED")
    for f in fails:
        print(f"  {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
