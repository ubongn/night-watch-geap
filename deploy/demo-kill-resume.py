"""Drive the local gateway through the kill-and-resume moment (demo rehearsal).

Starts a jam incident via POST /alerts, waits until the action has executed
(audit shows 'execution'), kills the run mid-incident, then resumes it and
waits for the verified completion. Leaves the run journal + audit chain in
place for the dashboard screenshots.
"""

import json
import sys
import time
import urllib.request

BASE = "http://localhost:8080"
JAM = json.load(open("evals/fixtures/webhook-jam.json", encoding="utf-8"))


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def audit_events(run_id):
    _, body = call("GET", "/audit?limit=200")
    return [r for r in body.get("records", []) if r.get("run_id") == run_id]


def main():
    # 0. inject the fault on the action plane
    inj = urllib.request.Request(
        "http://localhost:7822/control/fault",
        data=json.dumps({"kind": "conveyor_jam", "target": "sorter-conveyor",
                         "params": {"dock": "dock-3"}}).encode(),
        headers={"content-type": "application/json",
                 "authorization": "Bearer night-watch-dev"},
        method="POST",
    )
    urllib.request.urlopen(inj, timeout=10).read()
    print("fault injected: conveyor_jam @ sorter-conveyor")

    # 1. fire the alert
    status, body = call("POST", "/alerts", JAM)
    assert status == 202, (status, body)
    run_id = body["run_id"]
    print("run started:", run_id)

    # 2. wait for the action to execute
    deadline = time.time() + 40
    while time.time() < deadline:
        evs = [r["event"] for r in audit_events(run_id)]
        if "execution" in evs:
            break
        time.sleep(0.2)
    print("action executed. audit events so far:",
          [r["event"] for r in audit_events(run_id)])

    # 3. kill mid-incident
    status, body = call("POST", f"/runs/{run_id}/kill")
    print("killed:", status, body)

    # 4. resume
    time.sleep(1.0)
    status, body = call("POST", f"/runs/{run_id}/resume")
    print("resume:", status, body)

    # 5. wait for verified completion
    deadline = time.time() + 90
    while time.time() < deadline:
        _, st = call("GET", f"/runs/{run_id}")
        if st.get("status") != "running":
            break
        time.sleep(0.3)
    print("final:", json.dumps(st, indent=1))

    _, chain = call("GET", "/audit/verify")
    print("audit:", chain)
    ok = st.get("status") == "completed" and st.get("attempts", 0) >= 2 \
        and st.get("outcome") == "remediated" and chain.get("verified")
    print("KILL-AND-RESUME DEMO:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
