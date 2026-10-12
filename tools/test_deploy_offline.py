"""Offline checks for deploy_qwen38_27b (no network). Run: python3 -I tools/test_deploy_offline.py

2026-10-13 (promoted tmp/ -> tools/): dropped the old section 2, which cloned a
live group via `deploy_qwen38_27b.build_request` — that helper is gone (the
deployer now builds CreateContainerGroupRequest from CLI args in main(), it no
longer clones). The section imported a name that no longer exists, so the suite
crashed at import; the two sections that test live code are kept.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "deploy"))

from salad_client import CreateContainerGroupRequest, GpuClassInfo  # noqa: E402
from deploy_qwen38_27b import resolve_gpu_class  # noqa: E402

# --- 1. GPU resolution (incl. the 'RTX 5090 Laptop' trap) -------------------
classes = (
    GpuClassInfo(id="a5db5c50-cbcb-4596-ae80-6a0c8090d80f", name="RTX 3090 (24 GB)"),
    GpuClassInfo(id="851399fb-7329-4195-a042-d6514b28cf33", name="RTX 5090 (32 GB)"),
    GpuClassInfo(id="83ef776e-ce34-4d89-8cf9-81898f1416fa", name="RTX 5090 Laptop (24 GB)"),
    GpuClassInfo(id="9998fe42-04a5-4807-b3a5-849943f16c38", name="RTX 3090 Ti (24 GB)"),
)
assert resolve_gpu_class("rtx3090", classes).id == "a5db5c50-cbcb-4596-ae80-6a0c8090d80f"
assert resolve_gpu_class("rtx5090", classes).id == "851399fb-7329-4195-a042-d6514b28cf33"
# exact-match semantics: laptop/ti variants match only their own normalized names
assert resolve_gpu_class("rtx5090laptop", classes).id == "83ef776e-ce34-4d89-8cf9-81898f1416fa"
assert resolve_gpu_class("rtx3090ti", classes).id == "9998fe42-04a5-4807-b3a5-849943f16c38"
for bad in ("rtx4090", "rtx5090laptopextra"):
    try:
        resolve_gpu_class(bad, classes)
        raise AssertionError(f"{bad} should not match")
    except ValueError:
        pass
print("GPU resolution OK")

# --- 2. validation -----------------------------------------------------------
def make(**kw):
    base = dict(
        name="qwen38-27b", autostart_policy=False, container_image="img:latest",
        environment_variables={"A": "1"}, cpu=8, memory_mb=12288, replicas=2,
        restart_policy="always",
    )
    base.update(kw)
    return CreateContainerGroupRequest(**base)


make()  # valid
make(name="qwen38_27b", relaxed_name=True)  # underscore fallback only when relaxed
for bad in (dict(name="Qwen38-27b"), dict(name="qwen38_27b"), dict(replicas=501),
            dict(restart_policy="sometimes"), dict(priority="urgent"), dict(cpu=0),
            dict(memory_mb=512), dict(environment_variables={"A": ""})):
    try:
        make(**bad)
        raise AssertionError(f"should have rejected {bad}")
    except ValueError:
        pass
try:
    CreateContainerGroupRequest(
        name="qwen38-27b", autostart_policy=False, container_image="img:latest",
        environment_variables={}, cpu=8, memory_mb=12288, replicas=2, restart_policy="always",
        networking={"port": 80},  # missing auth/protocol
    )
    raise AssertionError("networking without auth/port/protocol should fail")
except ValueError:
    pass
print("validation OK")

print("ALL OFFLINE CHECKS PASSED")
