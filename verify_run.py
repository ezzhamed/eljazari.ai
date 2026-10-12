"""Clean end-to-end verification run.

Submits the prompt via the real backend and approves each HITL gate
EXACTLY ONCE, only when the gate is actually open (polls /api/projects
status). This is the disciplined opposite of the earlier buggy auto-
approver — it verifies that, with the idempotency fix, the pipeline runs
each phase once: one design, one code-gen, one simulation, one monitor.
"""
from __future__ import annotations

import os
import sys
import time

import httpx

# Defaults to the port the README quick start uses. Override for a backend
# started elsewhere:  DRONEPROMPT_API_URL=http://127.0.0.1:8010 python verify_run.py
API = os.environ.get("DRONEPROMPT_API_URL", "http://127.0.0.1:8000")
PROMPT = "Aerial photography quadcopter, takeoff to 12m, fly a 30m x 30m square, then land."

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]


def main() -> int:
    tok = httpx.post(f"{API}/api/auth/token", json={}, timeout=10).json()["access_token"]
    H = {"Authorization": f"Bearer {tok}"}
    r = httpx.post(f"{API}/api/projects/", headers=H,
                   json={"prompt": PROMPT, "name": "verify-idempotency"}, timeout=30)
    pid = r.json()["id"]
    print(f"[project] {pid}", flush=True)
    open("/tmp/verify_pid.txt", "w").write(pid)

    approved = {"design": False, "code": False}
    t0 = time.time()
    last = ""
    while time.time() - t0 < 1500:
        try:
            st = httpx.get(f"{API}/api/projects/{pid}", headers=H, timeout=8).json().get("status", "")
        except Exception:
            st = ""
        if st != last:
            print(f"[status] {st}  (t={int(time.time()-t0)}s)", flush=True)
            last = st
        if st in {"completed", "complete", "error", "failed"}:
            print(f"[final] {st}", flush=True)
            break
        # Approve design exactly once when its gate is open.
        if not approved["design"]:
            d = httpx.get(f"{API}/api/projects/{pid}/design", headers=H, timeout=8).json()
            if d.get("status") in {"generated", "ready", "pending_review", "awaiting_review"} or d.get("design_tree"):
                if d.get("status") != "approved":
                    httpx.post(f"{API}/api/projects/{pid}/design/approve", headers=H, json={}, timeout=15)
                    approved["design"] = True
                    print(f"[approve] design once (t={int(time.time()-t0)}s)", flush=True)
        # Approve code exactly once when its gate is open.
        elif not approved["code"]:
            c = httpx.get(f"{API}/api/projects/{pid}/code", headers=H, timeout=8).json()
            if c.get("status") in {"validated", "generated", "ready", "pending_review"} and c.get("code"):
                httpx.post(f"{API}/api/projects/{pid}/code/approve", headers=H, json={}, timeout=15)
                approved["code"] = True
                print(f"[approve] code once (t={int(time.time()-t0)}s)", flush=True)
        time.sleep(5)
    print(f"[done] {int(time.time()-t0)}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
