#!/usr/bin/env python3
"""Write live console grants back into version control (ADR 0014).

    python scripts/reconcile_overlay.py --overlay /var/lib/governed-metrics/overlay.jsonl
    python scripts/reconcile_overlay.py --overlay ... --write

Reads the overlay's effective events and updates the policy's machine-maintained include file
(the first entry under `include:` in the policy):

* a grant for a user version control does not list is added, with who granted it, when and why;
* a revoke removes the user from the include file. A user granted in the hand-written policy
  file is reported instead, because that change is a human edit, reviewed like any other.

Without --write it prints the plan and changes nothing. After the change merges and deploys,
the server sees version control governing those users and retires their overlay history
itself, so the overlay never becomes the source of truth.
"""
from __future__ import annotations

import argparse
import os
import sys

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

from governed_metrics import overlay  # noqa: E402

HEADER = ("# Maintained by scripts/reconcile_overlay.py. Console grants land here with who granted\n"
          "# them, when, and why, and are then reviewed like any other change.\n")


def plan(policy_path: str, overlay_path: str) -> tuple[str, dict, list[str], list[str]]:
    with open(policy_path, encoding="utf-8") as f:
        policy = yaml.safe_load(f) or {}
    includes = policy.get("include") or []
    if not includes:
        raise SystemExit("the policy has no `include:` file to reconcile into")
    grants_path = os.path.join(os.path.dirname(policy_path), includes[0])
    granted = {}
    if os.path.exists(grants_path):
        with open(grants_path, encoding="utf-8") as f:
            granted = (yaml.safe_load(f) or {}).get("users") or {}
    hand_written = {str(u).lower() for u in (policy.get("users") or {})}
    actions, manual = [], []
    for user, event in sorted(overlay.effective(overlay_path).items()):
        if event["action"] == "grant" and user not in hand_written and user not in granted:
            granted[user] = {"units": sorted(event.get("units") or []), "granted_by": event.get("by"),
                             "granted_at": event.get("at"), "reason": event.get("reason")}
            actions.append(f"add {user}: {', '.join(granted[user]['units'])}")
        elif event["action"] == "revoke":
            if user in granted:
                del granted[user]
                actions.append(f"remove {user}")
            if user in hand_written:
                manual.append(f"remove {user} from {os.path.basename(policy_path)} by hand (revoked "
                              f"by {event.get('by')} on {event.get('at')}: {event.get('reason')})")
    return grants_path, granted, actions, manual


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--policy", default=os.path.join(ROOT, "examples", "access.yaml"))
    ap.add_argument("--overlay", required=True)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)
    grants_path, granted, actions, manual = plan(args.policy, args.overlay)
    for a in actions:
        print(a)
    for m in manual:
        print(f"MANUAL: {m}")
    if not actions:
        print("nothing to reconcile")
    elif args.write:
        with open(grants_path, "w", encoding="utf-8") as f:
            f.write(HEADER + yaml.safe_dump({"users": granted}, sort_keys=True, default_flow_style=False))
        print(f"wrote {os.path.relpath(grants_path)}; review and commit it")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
