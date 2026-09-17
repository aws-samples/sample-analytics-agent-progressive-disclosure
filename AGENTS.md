# AGENTS.md

## Project Context

- This repository is an analytics agent demo built around progressive disclosure:
  the agent reads the `knowledge/` Markdown tree before writing SQL.
- The data backend is Athena over S3 Tables/Iceberg with Glue Data Catalog.
  Redshift and Aurora are fully retired: `database/redshift/`, `scripts/redshift/`,
  and `scripts/glue/` are dead paths — do not follow them. The live code is
  `backend/run.sh`, `backend/db.py`, and `scripts/lakehouse/`.
  Two files intentionally keep Redshift wording as history and carry a banner
  saying so: `docs/architecture-v2-redshift-glue.md` and `docs/legacy.md`.
- The governance layer (L4) **is** implemented, as a least-privilege IAM role
  (`analytics-agent-ro`) plus Lake Formation column-level grants — see
  `scripts/lakehouse/governance.py`. `user_messages` is not granted at all;
  `users.email` / `users.phone` / `user_profiles.birth_date` are excluded from the
  grant. Describe it as **column-level exclusion, not masking**: Lake Formation has
  no value-masking primitive, so those columns are absent from `SELECT *` and naming
  one returns `COLUMN_NOT_FOUND` — it is not v2's `***@masked.invalid` behaviour.
  It only applies when `AGENT_ROLE_ARN` is set; without it the backend queries with
  its own (usually admin) credentials and there is no column-level boundary.
- `analyticsagent/app/analytics/` (the AgentCore Runtime copy) contains **generated**
  files: `db.py`, `tools.py`, `athena.py`, `metric_layer.py`, `metrics_def.py`, `stats.py`
  are byte-for-byte copies, and `agent.py`'s prompts plus shared helpers are synced
  node-by-node, all by `scripts/deploy/sync_agent_code.py`. **Edit the source under
  `backend/` (or `scripts/lakehouse/athena.py`), then run `--apply`** — never edit the
  copy. It used to be a hand copy and drifted badly: the cloud prompt still taught a
  per-table `max(dt)` time anchor, so the same question answered a normal seven-figure
  total locally and **0** in the cloud. (Don't hardcode the expected figure — an earlier
  revision of this file pinned it, and the number moved when the data was reloaded.
  Compare local vs. cloud by recomputing both; see `analyticsagent/README.md`.)
  L0 runs `--check`; L8's `cloud-copy-drift` guards that check.
  `agent.py` is only partially synced on purpose: the drivers differ (local `run_agent`
  vs. the cloud's warm-client `build_options` / `stream_events`); the prompts must not.
- Python must be 3.11. Use `backend/.venv/bin/python`; do not use the system
  `python3` if it resolves to Python 3.9.
- The default local server command is `bash backend/run.sh` from the repository root.
  It requires valid AWS credentials and Bedrock model access, and opens
  `http://127.0.0.1:8000/`.
- The legacy local Postgres path is explicit: run `bash setup_local.sh` first, then
  `DB_BACKEND=postgres bash backend/run.sh`.
- Athena SQL uses Trino syntax. Avoid Postgres/Redshift-only constructs such as
  `::` casts, `DISTINCT ON`, and date arithmetic like `date + 7`.
- `.env.local` is gitignored and may contain account-specific values. Do not print
  or edit secrets; use `.env.local.example` for documented knobs.
- Live Bedrock/Athena runs may incur cost. Prefer static checks unless the task
  specifically needs an end-to-end agent invocation.

## Useful Commands

- `bash backend/run.sh` starts the current Athena/Iceberg backend.
- `DB_BACKEND=postgres bash backend/run.sh` starts the legacy local Postgres backend.
- `backend/.venv/bin/python -m compileall -q backend` checks backend syntax.
- `backend/.venv/bin/python backend/test_agent.py "最近 7 天每天的 DAU 是多少？"`
  runs a minimal agent smoke question.
- `bash scripts/test_all.sh` runs the full local test suite (L0–L6).
- `bash scripts/test_all.sh --l0` runs only L0 — no AWS calls, no credentials, and it
  exits 0 when L0 is green (the full run exits 1 at the AWS-identity gate without
  credentials, so its exit code cannot express "L0 passed"). This is what CI runs
  (`.github/workflows/offline.yml`: `--l0` + `negative_tests.py --offline` + the CDK
  policy assertions). CI covers **nothing** above L0 — see `docs/test-plan.md`.
- `bash scripts/test_all.sh --l8` appends the fault-injection negative tests
  (`scripts/negative_tests.py`, 49 cases): it temporarily edits repo files and restores
  them with a sha256 check. Layer meanings and the gaps with **no** automated coverage
  are in `docs/test-plan.md`.
- `backend/.venv/bin/python backend/db.py` runs the read-only SQL guard self-test.
  It enforces **write** denial; L4 governance enforces **read** denial. The two are not
  substitutes for each other — do not weaken one because the other exists.
- `backend/.venv/bin/python scripts/deploy/sync_agent_code.py --check` verifies the
  AgentCore copy under `analyticsagent/app/analytics/` still matches `backend/`.

## Editing Expectations

- Keep changes scoped to the requested area.
- When changing prompts, metrics, or SQL behavior, check the related files in
  `knowledge/`, `backend/agent.py`, `backend/metrics_def.py`, and `backend/db.py`.
  Prompt or shared-module changes also need `scripts/deploy/sync_agent_code.py --apply`,
  and prompt changes need a full L7 run (`eval/run_eval.py`, 27 cases) — that suite is
  the only thing covering `backend/agent.py`.
- Preserve the read-only SQL boundary in `backend/db.py`.
- Preserve the **tool boundary** in `backend/agent.py`: the agent may only call the five
  `mcp__analytics__*` tools plus `ToolSearch`. The gate is a **`PreToolUse` hook**, not
  `can_use_tool` — the latter is silently shadowed by `permission_mode="bypassPermissions"`
  and never fires, which left every built-in tool (Bash / Read / Write / Edit, 25 reachable
  in total) auto-approved and made `db.py`'s SQL guard bypassable. `disallowed_tools`
  (`DENIED_BUILTINS`) is the second layer. `ToolSearch` **must** stay allowed: MCP tools are
  lazily loaded, so denying it locks the agent out of its own tools. Guarded by
  `backend/agent.py --selftest` (L0) and the L8 cases `tool-gate-shadowed` /
  `tool-gate-toolsearch-locked`.
