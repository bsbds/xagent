# MCP API operational behavior

## Disconnect conflicts

`DELETE /api/mcp/servers/{server_id}` returns `204` when disconnect succeeds. It returns `409` when a non-default resource owner still depends on the server. The response has a stable dependency class:

```json
{
  "detail": {
    "code": "mcp_disconnect_dependency",
    "message": "MCP server has non-default owner dependencies",
    "dependency_class": "active_oauth_grant"
  }
}
```

`dependency_class` determines remediation:

- `active_oauth_grant`: revoke the blocking grant with `DELETE /api/mcp/{server_id}/oauth/grants/{grant_id}`, then retry disconnect.
- `unconsumed_oauth_flow`: let the live authorization flow finish or expire, then retry. Expired flow state does not block disconnect.
- `actor_oauth_credential`: no public actor-credential release operation exists yet. The trusted integration that owns the actor must retain the server link; lifecycle-safe release is tracked in [issue #1511](https://github.com/xorbitsai/xagent/issues/1511).

The conflict preflight runs before any association, grant, credential, or shared-server mutation.
