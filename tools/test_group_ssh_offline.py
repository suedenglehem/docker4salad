#!/usr/bin/env python3
"""Offline suite for the GROUP positional + the `ssh` action in utils/sl_admin.py.

Zero API calls: the Salad client is monkeypatched and the ssh spawn
(subprocess.run) is faked. Run:  python3 -I tools/test_group_ssh_offline.py

Covers:
  * the 3rd positional (GROUP) selecting a group directly for the group-targeting
    actions (start/stop/wait/stats/delete/describe/ssh), and the ORG/PROJECT
    form of the 2nd positional;
  * `_resolve_group` — direct GET when the project is named, project search when
    it is not (found / ambiguous / not-found);
  * the `ssh` action — running group with a live instance (the exact ssh argv),
    not-running group, and no-live-instance group;
  * the flag/positional ordering (… GROUP --min-bw-mbps N still parses).
"""
from __future__ import annotations

import importlib.util
import os
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


# --- fakes --------------------------------------------------------------------

class FakeGroup:
    """Minimal stand-in for ContainerGroupInfo (name/status)."""

    def __init__(self, raw: dict):
        self.raw = raw
        self.name = raw["name"]
        self.current_status = (raw.get("current_state") or {}).get("status")


class FakeInstance:
    def __init__(self, id, state, ready, ssh_ip=None, ssh_port=None, fp=None):
        self.id = id
        self.state = state
        self.ready = ready
        self.ssh_ip = ssh_ip
        self.ssh_port = ssh_port
        self.ssh_host_key_fingerprint = fp


def make_group(name: str, status: str = "stopped") -> dict:
    return {"name": name, "current_state": {"status": status}}


class FakeAPI:
    """Fakes the client calls the group/ssh path uses; records every call.

    groups_by_scope: {(org, project): [group raw, ...]}
    instances:       {(org, project, group): [FakeInstance, ...]}
    """

    def __init__(self, groups_by_scope: dict, instances: dict):
        self.groups_by_scope = groups_by_scope
        self.instances = instances
        self.calls: list = []

    def install(self) -> list:
        def get_container_group(org, project, name, api_key=None):
            self.calls.append(("get", org, project, name))
            for g in self.groups_by_scope.get((org, project), []):
                if g["name"] == name:
                    return FakeGroup(g)
            raise sc.SaladApiError(404, None, "{}")

        def list_container_groups(org, project, api_key=None):
            self.calls.append(("list", org, project))
            return tuple(FakeGroup(g) for g in self.groups_by_scope.get((org, project), []))

        def list_container_group_instances(org, project, name, api_key=None):
            self.calls.append(("instances", org, project, name))
            return tuple(self.instances.get((org, project, name), []))

        sc.get_container_group = get_container_group
        sc.list_container_groups = list_container_groups
        sc.list_container_group_instances = list_container_group_instances
        sc.load_api_key = lambda: "fake-key"
        return self.calls


class FakeSubprocess:
    """Stands in for the subprocess module; records the ssh argv it is asked to run."""

    def __init__(self, returncode: int = 0):
        self.runs: list = []
        self.returncode = returncode

    def run(self, cmd, **kw):
        self.runs.append(cmd)

        class _C:
            returncode = self.returncode
        return _C()


def answer(*replies):
    """Feed canned stdin to input() (a module-level name shadows the builtin)."""
    it = iter(replies)

    def _input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    ADMIN.input = _input


def main() -> int:
    fleet = ADMIN._fleet()

    # --- 1. _resolve_group: project named -> direct GET ----------------------
    api = FakeAPI({(ORG, PROJ): [make_group("atx-swift-27b-q5", "running")]}, {})
    api.install()
    r = ADMIN._resolve_group(ORG, PROJ, "atx-swift-27b-q5", "k", fleet)
    check("resolve: named project -> (org, project, group)",
          r is not None and r[0] == ORG and r[1] == PROJ and r[2].name == "atx-swift-27b-q5",
          repr(r))
    check("resolve: named project uses a direct GET (no project search)",
          api.calls == [("get", ORG, PROJ, "atx-swift-27b-q5")], repr(api.calls))

    r = ADMIN._resolve_group(ORG, PROJ, "no-such-group", "k", fleet)
    check("resolve: named project, missing group -> None", r is None)

    # --- 2. _resolve_group: project omitted -> search the org's projects -----
    api = FakeAPI({(ORG, "qwen38-27b"): [], (ORG, PROJ): [make_group("atx-swift-27b-q5", "running")]}, {})
    api.install()
    r = ADMIN._resolve_group(ORG, None, "atx-swift-27b-q5", "k", fleet)
    check("resolve: no project -> searches and finds it in llm",
          r is not None and r[1] == PROJ and r[2].name == "atx-swift-27b-q5", repr(r))
    check("resolve: no project issues a list per project",
          [c[0] for c in api.calls] == ["list", "list"], repr(api.calls))

    api = FakeAPI({(ORG, "qwen38-27b"): [make_group("dup")], (ORG, PROJ): [make_group("dup")]}, {})
    api.install()
    r = ADMIN._resolve_group(ORG, None, "dup", "k", fleet)
    check("resolve: no project, name in two projects -> None (ambiguous)", r is None)

    api = FakeAPI({(ORG, "qwen38-27b"): [], (ORG, PROJ): []}, {})
    api.install()
    r = ADMIN._resolve_group(ORG, None, "ghost", "k", fleet)
    check("resolve: no project, nowhere -> None (not found)", r is None)

    # --- 3. main(): the GROUP positional selects directly (no table) ---------
    # start ORG/PROJ GROUP -> resolves via GET, then aborts at the confirm (answer "n").
    api = FakeAPI({(ORG, PROJ): [make_group("atx-swift-27b-q5", "stopped")]}, {})
    api.install()
    answer("n")
    rc = ADMIN.main(["sl_admin.py", "start", f"{ORG}/{PROJ}", "atx-swift-27b-q5"])
    check("main: start ORG/PROJ GROUP resolves via GET (rc=1 = aborted at confirm)",
          rc == 1, f"rc={rc}")
    check("main: start ORG/PROJ GROUP made a direct GET, no table list",
          api.calls == [("get", ORG, PROJ, "atx-swift-27b-q5")], repr(api.calls))

    # start ORG GROUP (no project) -> searches projects, then aborts at confirm.
    api = FakeAPI({(ORG, "qwen38-27b"): [], (ORG, PROJ): [make_group("atx-swift-27b-q5", "stopped")]}, {})
    api.install()
    answer("n")
    rc = ADMIN.main(["sl_admin.py", "start", ORG, "atx-swift-27b-q5"])
    check("main: start ORG GROUP searches projects (rc=1 = aborted at confirm)", rc == 1, f"rc={rc}")
    check("main: start ORG GROUP issued a per-project list",
          [c[0] for c in api.calls] == ["list", "list"], repr(api.calls))

    # A group positional on a non-group action is a usage error (rc=2), no API.
    rc = ADMIN.main(["sl_admin.py", "list", f"{ORG}/{PROJ}", "atx-swift-27b-q5"])
    check("main: list ORG/PROJ GROUP -> usage error (rc=2)", rc == 2, f"rc={rc}")

    # Too many positionals is a usage error (rc=2), no API.
    rc = ADMIN.main(["sl_admin.py", "start", f"{ORG}/{PROJ}", "grp", "extra"])
    check("main: start ORG/PROJ GROUP extra -> usage error (rc=2)", rc == 2, f"rc={rc}")

    # A group positional with no org is a usage error (rc=2).
    rc = ADMIN.main(["sl_admin.py", "start", "atx-swift-27b-q5"])
    check("main: start GROUP (no org) -> usage error (rc=2)", rc == 2, f"rc={rc}")

    # --- 4. main(): flags after the positionals still parse ------------------
    # start ORG/PROJ GROUP --min-bw-mbps 100 -> the flag is consumed (not a 3rd
    # positional), the group resolves, then it aborts at the confirm (answer "n").
    api = FakeAPI({(ORG, PROJ): [make_group("atx-swift-27b-q5", "stopped")]}, {})
    api.install()
    answer("n")
    rc = ADMIN.main(["sl_admin.py", "start", f"{ORG}/{PROJ}", "atx-swift-27b-q5",
                     "--min-bw-mbps", "100"])
    check("main: start ORG/PROJ GROUP --min-bw-mbps 100 parses (rc=1 = aborted at confirm)",
          rc == 1, f"rc={rc}")
    check("main: … --min-bw-mbps 100 resolved the group via GET",
          api.calls == [("get", ORG, PROJ, "atx-swift-27b-q5")], repr(api.calls))

    # --- 5. the ssh action ---------------------------------------------------
    # running group + a live instance -> the exact ssh argv is spawned.
    live = FakeInstance("abc12345", "running", True, ssh_ip="10.1.2.3", ssh_port=2222,
                        fp="SHA256:deadbeef")
    api = FakeAPI({(ORG, PROJ): [make_group("atx-swift-27b-q5", "running")]},
                  {(ORG, PROJ, "atx-swift-27b-q5"): [live]})
    api.install()
    sp = FakeSubprocess(returncode=0)
    ADMIN.subprocess = sp
    rc = ADMIN.main(["sl_admin.py", "ssh", f"{ORG}/{PROJ}", "atx-swift-27b-q5"])
    check("ssh: running group + live instance -> rc=0", rc == 0, f"rc={rc}")
    check("ssh: spawned the interactive ssh argv",
          sp.runs == [["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                       "-o", "ConnectTimeout=15", "-p", "2222", "root@10.1.2.3"]],
          repr(sp.runs))

    # not-running group -> error, no ssh spawned, no instance listing.
    api = FakeAPI({(ORG, PROJ): [make_group("atx-swift-27b-q5", "stopped")]}, {})
    api.install()
    sp = FakeSubprocess()
    ADMIN.subprocess = sp
    rc = ADMIN.main(["sl_admin.py", "ssh", f"{ORG}/{PROJ}", "atx-swift-27b-q5"])
    check("ssh: not-running group -> rc=1", rc == 1, f"rc={rc}")
    check("ssh: not-running group spawns no ssh", sp.runs == [], repr(sp.runs))

    # running group but no live instance (still downloading) -> error, no ssh.
    busy = FakeInstance("def67890", "downloading", False)
    api = FakeAPI({(ORG, PROJ): [make_group("atx-swift-27b-q5", "running")]},
                  {(ORG, PROJ, "atx-swift-27b-q5"): [busy]})
    api.install()
    sp = FakeSubprocess()
    ADMIN.subprocess = sp
    rc = ADMIN.main(["sl_admin.py", "ssh", f"{ORG}/{PROJ}", "atx-swift-27b-q5"])
    check("ssh: running group, no live instance -> rc=1", rc == 1, f"rc={rc}")
    check("ssh: no live instance spawns no ssh", sp.runs == [], repr(sp.runs))

    # ssh propagates the ssh exit code.
    live = FakeInstance("abc12345", "running", True, ssh_ip="10.1.2.3", ssh_port=2222)
    api = FakeAPI({(ORG, PROJ): [make_group("atx-swift-27b-q5", "running")]},
                  {(ORG, PROJ, "atx-swift-27b-q5"): [live]})
    api.install()
    sp = FakeSubprocess(returncode=255)
    ADMIN.subprocess = sp
    rc = ADMIN.main(["sl_admin.py", "ssh", f"{ORG}/{PROJ}", "atx-swift-27b-q5"])
    check("ssh: propagates the ssh exit code", rc == 255, f"rc={rc}")

    # --- 6. describe: the positional group (search + pinned project) --------
    api = FakeAPI({(ORG, "qwen38-27b"): [], (ORG, PROJ): [make_group("atx-swift-27b-q5", "stopped")]}, {})
    api.install()
    described: list = []
    ADMIN._describe = lambda org, project, name, key, raw: (described.append((org, project, name)), 0)[1]
    rc = ADMIN.main(["sl_admin.py", "describe", ORG, "atx-swift-27b-q5"])
    check("describe: ORG GROUP searches and describes the found project",
          rc == 0 and described == [(ORG, PROJ, "atx-swift-27b-q5")],
          f"rc={rc} described={described}")

    api = FakeAPI({(ORG, PROJ): [make_group("atx-swift-27b-q5", "stopped")]}, {})
    api.install()
    described.clear()
    rc = ADMIN.main(["sl_admin.py", "describe", f"{ORG}/{PROJ}", "atx-swift-27b-q5"])
    check("describe: ORG/PROJ GROUP describes directly (no project search)",
          rc == 0 and described == [(ORG, PROJ, "atx-swift-27b-q5")],
          f"rc={rc} described={described}")

    # --- summary -------------------------------------------------------------
    if fails:
        print(f"\n{len(fails)} failure(s)")
        for f in fails:
            print(f"  FAIL {f}")
        return 1
    print("\nALL GROUP/SSH CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
