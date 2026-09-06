# Demo walkthrough

All accounts, project examples, and monitoring signals in this walkthrough are synthetic.

1. Start the Compose stack and sign in as `admin` at http://localhost:8080.
2. Open Projects and inspect the seeded walkthrough project. Review its product, owner, application binding, scheduled job, and recovery scenarios.
3. Explore handover and verification using `testuser`. Inspect the available on-duty schedules and support ownership from the administrator account.
4. Open the scheduler mock console at http://localhost:9000. Select a seeded job and change its simulated status. Allow a monitoring interval for the result to reach RelayOps.
5. Inspect the generated issue: its reason, assignment, recovery instructions, SLA, and audit history. Restore the simulated service to explore recovery.
6. For model monitoring, the walkthrough job binds to `inventory-risk-demo / agg-v2`. Use http://localhost:9005/docs to call `POST /api/demo/signals`. Set the Authorization header to `Bearer relayops-demo-monitor-token` and submit `{"drifted":true}`. Within the configured monitoring interval, inspect the model-drift issue. Repeat with `{"drifted":false}` for recovery.
7. Optionally configure an LLM and use the assistant to inspect a project or prepare a change. Review the proposal before confirming it. Without an LLM, model-dependent actions report that configuration is needed.

Outgoing messages use the log backend. They are previews and are not delivered to recipients. Synthetic monitoring changes reset when the corresponding mock process restarts; the main database uses a persistent Docker volume.
