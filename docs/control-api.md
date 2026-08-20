# Task control API behavior

## Pause

A policy-marked task uses cache-only pause. The command acts only on an owner-matched agent that is live in the current process. It does not construct tools, resolve connector credentials, or create a workspace.

An unmarked task keeps the historical pause behavior. The worker can reconstruct an unmarked task when no cached agent exists.

When a policy-marked task has no live cached execution, the WebSocket control response is:

```json
{
  "type": "error",
  "message": "No live execution found to pause"
}
```

Callers can retry after the policy-marked task has a live execution. A persisted `RUNNING` status does not make that cold task pausable.
