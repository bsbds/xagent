# Deployment changes

## 2026-08-11 — New public-task File Operation isolation

### Deployment impact

New widget and shared-link tasks use a server-owned policy marker to restrict File Operation access to the task owner and exact task. Existing private tasks and historical public tasks remain unmarked and keep their previous behavior.

A2A-, SDK-, and trigger-created tasks also remain unmarked and retain legacy owner-wide File Operation behavior. Protect runtime API keys and externally callable trigger credentials accordingly.

A mixed-version deployment is unsafe after new public task creation starts. An older worker does not enforce the marker. Gate widget and shared-link task creation until all API and task-execution workers run the new version.

### Scope limitation

This rollout isolates only the File Operation tool family. Other tools that read paths from the shared task workspace, including image, audio, PowerPoint, video, and SSH upload tools, continue to use the existing workspace resolver and owner-wide external directory roots. MCP roots, sandbox access, shell and Python execution, knowledge-base operations, and preview/download authorization are also unchanged.

Do not treat this rollout as complete public-file isolation. Restrict those tools separately when a public deployment requires a task-wide boundary across every file-capable tool.

Isolation is task-level, not agent-level: delegated agents within the same task inherit the parent task identity and share that task's File Operation file set.

### Prerequisites and configuration

This change has no database migration, backfill, new environment variable, dependency, or infrastructure requirement.

Before deployment, inspect existing `Task.agent_config` values for `__xagent_file_operation_access_version`. Use the query for the configured database:

```sql
-- PostgreSQL
SELECT id
FROM tasks
WHERE agent_config -> '__xagent_file_operation_access_version' IS NOT NULL
LIMIT 1;
```

```sql
-- SQLite with the JSON1 extension
SELECT id
FROM tasks
WHERE json_type(agent_config, '$.__xagent_file_operation_access_version') IS NOT NULL
LIMIT 1;
```

Both queries must return no rows. If either query returns a task, stop the deployment. Select an unused internal key before you continue.

### Deployment and migration steps

1. Gate new widget and shared-link task creation.
2. Deploy the same application version to all API and task-execution workers.
3. Make sure that no old worker can receive a newly created public task.
4. Re-enable widget and shared-link task creation.

Do not backfill historical tasks. Marker absence is the compatibility boundary for this rollout.

For a future marker version, first deploy readers that accept both the current and
new versions while writers still emit the current version. Only change writers
after every API and task-execution worker accepts the new version. Never replace
the current accepted version in one step because persisted tasks must remain
readable throughout the rollout.

### Verification and monitoring

Create one widget task and one shared-link task after the rollout. Make sure that each task can use its own uploaded file.

Make sure that each task cannot use a same-owner file from another task by file ID. Repeat the check with a raw path.

Monitor task execution errors for File Operation policy failures. A failure on a newly created public task can indicate a malformed marker or missing task/owner authority.

### Per-task emergency remediation

If one task must recover before its policy inconsistency can be repaired, quiesce that task, remove only `__xagent_file_operation_access_version` from its `agent_config`, and rebuild or restart its execution. This opts that task out of exact-task isolation and restores legacy owner-wide File Operation access. Treat the change as an audited security exception because it reintroduces same-owner cross-task access for that task.

### Rollback

Gate new widget and shared-link task creation before rolling back any worker. Roll back all API and task-execution workers together. Do not re-enable public task creation while versions are mixed.

Marked tasks do not remain isolated when executed by an older worker. Keep public execution gated during rollback, or complete the forward rollout before those tasks resume.

## 2026-08-18 — Actor-owned builtin OAuth credentials

### Deployment impact

The `user_oauth` table gets a nullable `resource_owner_key` column. Existing rows keep a null value.

Two partial unique indexes replace `uq_user_provider_account`. One index protects ordinary rows. The other index protects actor-owned rows. SQLite and PostgreSQL are the only supported database dialects for this schema; startup and migration fail before schema creation on other dialects.

On PostgreSQL the migration inspects each replacement index in the system catalogs before use. A missing, invalid, non-unique, reordered, expression-based, wrong-predicate, or otherwise non-exact same-table index is dropped and recreated with `CONCURRENTLY`. The migration rechecks validity, uniqueness and null semantics, access method, ordered keys, included attributes, tablespace/storage options, and the normalized partial predicate for all three indexes before removing the old unique constraint. A same-name relation owned by another table stops the migration rather than dropping an unrelated object. On SQLite the table is rebuilt in batch mode and the partial indexes are installed in the same migration transaction.

A mixed-version deployment is unsafe after actor-owned rows exist. An old worker can read an actor-owned credential without the new owner filter.

Actor-aware callers stamp a server-owned task marker when the builtin OAuth policy must be present. The policy remains ephemeral. Resume and cold reconstruction without it fail closed instead of using an ordinary builtin credential.

Native MCP connectors keep their existing account-level configuration and credential behavior. The actor policy does not change native MCP selection.

### Prerequisites and configuration

This change has no new environment variable or dependency.

Stop all old API workers and task workers before the migration. Keep trusted actor builtin OAuth callers disabled during the deployment.

### Deployment and migration steps

1. Stop new OAuth connections and new task execution.
2. Stop all old API workers and task workers.
3. Deploy the new application files without starting the workers.
4. Run `alembic upgrade head` one time.
5. Start all API workers and task workers with the new version.
6. Enable trusted actor builtin OAuth callers only after all workers run the new version.
7. Enable new OAuth connections and task execution.

Do not backfill `resource_owner_key`. A null owner identifies an ordinary credential.

### Verification and monitoring

Run these queries after the migration:

```sql
SELECT count(*)
FROM user_oauth
WHERE resource_owner_key IS NOT NULL;
```

The result must be zero before an actor-aware product creates its first credential.

```sql
SELECT indexname
FROM pg_indexes
WHERE tablename = 'user_oauth'
  AND indexname IN (
    'uq_user_oauth_ordinary_account',
    'uq_user_oauth_actor_account',
    'ix_user_oauth_owner_provider'
  );
```

The query must return all three index names on PostgreSQL. Also verify validity (all rows must return `t`):

```sql
SELECT c.relname, i.indisvalid
FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid
WHERE c.relname IN (
  'uq_user_oauth_ordinary_account',
  'uq_user_oauth_actor_account',
  'ix_user_oauth_owner_provider'
);
```

For SQLite run `PRAGMA index_list('user_oauth');` and `PRAGMA index_info('<index-name>');`, and inspect `sqlite_master.sql` to confirm the ordinary index has `WHERE resource_owner_key IS NULL` and the actor index has `WHERE resource_owner_key IS NOT NULL`.

After actor builtin OAuth starts, connect the same builtin application for two actors. Make sure that each task receives only its actor credential. Attempt to resume a marked task without its actor policy. Make sure that agent construction rejects it.

Make sure that existing native MCP connectors still use their account-level configuration.

Monitor `actor_policy_conflict`, `actor_policy_requires_builtin_oauth`, `actor_policy_server_not_allowed`, and `oauth_token_required` diagnostics. An increase can show an incorrect actor policy, an incorrectly classified server, or a missing credential.

### Rollback

If no actor-owned row exists, stop all workers. Run `alembic downgrade 20260818_seed_jira_mcp_app`, then deploy the old version.

CAUTION: Do not run the downgrade after an actor-owned row exists. The migration stops because the old schema cannot preserve actor isolation.

If actor-owned rows exist, use a forward fix. Remove those rows only under an approved credential-revocation and data-removal procedure.
