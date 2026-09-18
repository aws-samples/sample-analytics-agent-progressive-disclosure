# AGENTS.md

## Project Context

- This repository is an analytics agent demo built around progressive disclosure:
  the agent reads the `knowledge/` Markdown tree before writing SQL.
- The backend the agent itself runs on is Athena over S3 Tables/Iceberg with Glue
  Data Catalog: `backend/run.sh`, `backend/db.py`, `scripts/lakehouse/`.
- **Three query arms are live in parallel, for architecture comparison.** They are
  not migration stages and none replaces another; each is measured against the
  other two on correctness first, then time and cost.
  1. **Athena + S3 Tables** — `scripts/lakehouse/`. Deployed. 35 base tables plus
     13 derived, 79,943,758 rows in namespace `app_analytics`.
  2. **DuckDB + S3 Tables** — `scripts/duckdb/`. Reads the **same** Iceberg tables
     as arm 1 through `ATTACH ... (TYPE iceberg, ENDPOINT_TYPE s3_tables)`, so it
     needs no storage of its own, only compute. `conn.py` is the connection layer
     (`--selftest`, `--tables`); `verify_load.py --rows-only` reconciles all 35
     tables against `data/loaded_row_counts.json`.
  3. **Redshift (Serverless)** — `database/redshift/`, `scripts/redshift/`,
     `scripts/glue/`. Deployed in us-west-2: workgroup `analytics-agent-wg`,
     namespace `analytics-agent-ns`, db `app_analytics`, **base 8 / max 8 RPU
     (pinned, so compute is repeatable across runs)**, `publiclyAccessible=false`.
     35 tables, 79,943,758 rows, loaded by `scripts/redshift/load_from_s3.py`
     from `s3://analytics-agent-raw/parquet/`. It is the one arm that needs a
     separate materialization, because `COPY` cannot read Iceberg — that extra
     copy is itself one of the comparison findings, not an oversight. Two knobs
     were set for fairness, not performance: `auto_mv=false` on the workgroup
     (neither other arm has automatic materialized views), and the session result
     cache is turned off by `scripts/bench/arms.py: configure_for_timing()`.
     Region is not a preference: Parquet `COPY` goes through Spectrum, Spectrum
     rejects `REGION`, so the bucket and the workgroup must share a region.
  Storage is shared as far down as possible on purpose: arms 1 and 2 read the same
  physical files, so a difference between them is a difference in the engine.
  `scripts/bench/` holds the shared harness: `arms.py` (one adapter per arm — the
  only per-arm differences are how to connect and how to spell the SQL),
  `correctness.py` (the table-level gate), `query_correctness.py` (the query-level
  gate), `prices.py` + `timing.py` (the time and cost scales), and `fargate.py` +
  `Dockerfile` (the same harness on Fargate). A third correctness layer lives with the
  eval suite rather than here: `eval/compare_goldens.py` compares the 27 goldens'
  **values** across arms, which is the only layer that exercises the computed mart tables.
  Correctness is
  a **gate, not a score**: it is never averaged with time or cost, because an arm that
  is wrong is not partly good.
- **The two correctness layers catch different things and neither subsumes the other.**
  `correctness.py` compares row counts, numeric sums, time bounds and boolean counts
  per table — so it says "the three arms hold the same data" and nothing about whether
  they answer the same question. `query_correctness.py` compares whole result sets for
  JOIN/window/NULL/time/division cases, by **three-way voting plus a per-case
  invariant**: voting alone is green whenever all three arms are wrong the same way, so
  every gate case also carries an independently computed expectation (row counts read
  from `data/loaded_row_counts.json`, never hardcoded). Cases are split into
  `must_match=True` gates and `must_match=False` probes; a probe's divergence is a
  **comparison finding**, not a failure, or the script would be permanently red and
  nobody would read it. Errors are treated as values, not crashes, because "raises" vs
  "returns NULL" is itself one of the differences being measured. `--values` is a
  separate mode that sweeps MIN/MAX of every text column: the table-level gate has no
  text-column metric at all, so a column that is a **fake constant in two arms** passes
  it silently — which is exactly how the `RELOAD_PENDING` divergence below was found.
  **Both correctness layers carry one exemption, and it is an assertion rather than a
  skip.** Redshift's dynamic masking applies *before* aggregation, so `users.email`,
  `users.phone` and `user_profiles.birth_date` are legitimately different on that arm;
  they are registered in `correctness.py: MASKED` (one registry, shared by both gates)
  and judged against the masking policy's own transform instead of against the other
  arms. `mask_email` and `mask_birthdate` are checked to the value — `DATE_TRUNC('year')`
  is monotone, so `max(trunc(x)) == trunc(max(x))` — while `mask_phone` splices
  characters 1–3 and 8–11 and is therefore **not** monotone, so it is checked to shape
  only: `users.phone`'s cross-arm equality is not verifiable through a `TO PUBLIC`-masked
  connection, and what the gate confirms there is that the mask is in effect. Clear arms
  are still compared with each other on those columns, so the exemption is not a
  whole-column blind spot. Without this, both gates were red with no defect present —
  the mirror image of a green light hiding one, and the reason a real divergence would
  have been buried under the same two lines.
- **Measured dialect divergences (from `query_correctness.py`, all reproducible).** These
  are comparison findings, not bugs, and they are what an agent trips over when the same
  question is asked on a different arm:
  1. **Redshift has no `RANGE` window frame** (`RANGE clause of window functions not yet
     implemented`) *and* rejects an aggregate window function that has `ORDER BY` but no
     frame. Since `RANGE ... CURRENT ROW` **is** the SQL standard default frame, the
     standard default is inexpressible on Redshift: a running total over a key with ties
     has no portable spelling. Only the `ROWS` form works on all three.
  2. `7/2` is `3` on Athena and Redshift, `3.5` on DuckDB.
  3. Division by zero raises on Athena (`DIVISION_BY_ZERO`) and Redshift, but returns
     `Infinity` on DuckDB — so the same bad query costs a retry on two arms and silently
     poisons a report on the third.
  4. `DECIMAL / INTEGER` with no cast diverges: `SUM(actual_amount / item_count)` gives
     140681679 on Athena vs 140681100.354 on DuckDB/Redshift. Ratio metrics need an
     explicit `CAST(... AS DOUBLE)`; and even then float sums differ past ~12 significant
     digits because accumulation order follows each engine's parallel plan, which is why
     that one gate case compares to 12 significant digits instead of bit-for-bit.
  `ROUND` half-up behaviour, CJK MIN/MAX ordering and `date_trunc` month bucketing all
  agree across the three, which is worth knowing because all three were suspected.
- **Governance, in one sentence** (verified on our own workgroup, not from docs):
  Redshift Serverless is the finest-grained of the three — table and column `GRANT`,
  row-level security (`svv_rls_policy`) and value-level dynamic masking
  (`svv_masking_policy`), so it is the only arm that can return a *masked value*; Athena
  plus Lake Formation reaches column level but only by **exclusion**, not masking; DuckDB
  has no governance layer of its own at all, since it runs in-process as whatever
  principal invoked it, so any boundary has to live in the surrounding application.
  (An earlier draft had this backwards, ranking Redshift as table-level only.)
- **The agent itself can run on any of the three arms**: `DB_BACKEND` selects one of
  `athena` / `duckdb` / `redshift` (plus `postgres`, the legacy local path), and an
  unrecognized value raises at import rather than falling through to a default.
  Switching arms changes exactly two things — the connection, and the SQL dialect
  appended to the prompt tail (`db.DIALECT`, consumed by `agent.py: _dialect()`).
  Everything else — the `knowledge/` tree, metric definitions, the eval questions,
  the judging criteria — stays identical, or the comparison measures the context
  layer instead of the engine. `db.DIALECTS["athena"]` is the **empty string** on
  purpose and `db.py --selftest` enforces it: that keeps the Athena arm's prompt
  byte-identical, so the existing L7 baseline (27 cases) stays a valid reference.
  The appendix goes at the tail rather than into `SYSTEM` for the same reason.
  Two consequences to know: `duckdb` and `redshift` have **no** column-level
  governance boundary, so `backend_info()["identity"]` is empty for them (visibly,
  not silently); and all three arms enforce a statement timeout but by three different
  mechanisms — Athena polls and calls `StopQueryExecution`, Redshift relies on the Data
  API polling timeout, and DuckDB needs a **client-side watchdog**: it has no session
  `statement_timeout` (`duckdb_settings()` has no such entry) but it does have
  `conn.interrupt()`, which `scripts/duckdb/conn.py: Client.execute(timeout=...)` calls
  from a `threading.Timer`. Measured on duckdb 1.5.5: a runaway query is killed at 2.0s
  with `InterruptException`, and the connection stays usable afterwards. So the
  difference between arms is implementation cost, not capability. (An earlier revision
  of this file claimed DuckDB had "no cancellable handle" — that was wrong.)
  The AgentCore copy runs the `athena` arm, and its warm client pins
  `system_prompt=SYSTEM` without the dialect tail; since the Athena appendix is the
  empty string, the two are equivalent today.
- **Timing runs need three knobs off, and each is verified by reading the engine
  back rather than by echoing intent** (`arms.py: configure_for_timing()`).
  Redshift's session result cache is the subtle one: every Data API
  `ExecuteStatement` is an **independent session**, so a bare `SET` evaporates —
  measured, `SHOW` returned `on` both before and after. It now runs inside a kept
  session (`rsql.Client(keepalive=...)` → `SessionKeepAliveSeconds`, then
  `SessionId` alone on later calls, which is mutually exclusive with
  WorkgroupName/Database/SecretArn) and raises if the readback disagrees. DuckDB's
  `DUCKDB_THREADS` defaults to `4` and is not "one per core": laptop and Fargate
  must be the same yardstick. The memory limit stays 4GB on Fargate even though the
  task has 16GB, because the limit decides when spilling starts and spilling
  dominates timing — two limits means comparing two execution plans.
- **DuckDB pins AWS credentials at `CREATE SECRET` time unless told otherwise** — it does
  not refresh them the way the boto3-backed arms do, so a sweep that outlives the
  credential lifetime dies part-way with `The security token included in the request is
  expired`. Measured on the 35-table `correctness.py --arms-only` run: it failed at table
  33 of 35 with 32 tables already green, i.e. not a data disagreement. **Fixed on two
  levels in `scripts/duckdb/conn.py`, and they cover different failures:**
  `REFRESH auto` on the secret re-runs the credential chain (one secret covers both the
  catalog and the data plane — `ATTACH ... (SECRET <name>)` is a real option, so the
  s3_tables mount goes through the same secret manager), and `Client.execute` reconnects
  once and retries when it recognizes an expired-credential error, which does not depend
  on any version's refresh behaviour and also covers an `ATTACH` already holding a stale
  handle. Two things worth knowing: **`REFRESH`'s value is not validated** — a bogus
  `REFRESH banana` binds without error and simply drops `refresh_info`, so
  `_assert_refresh()` reads the secret back instead of trusting the `CREATE` (a
  misspelled parameter *name* does raise `Binder Error`; the value does not); and a
  retried statement returns `reconnected: True`, whose `elapsed_ms` includes the
  reconnect plus both attempts, so **timing runs must discard that sample**. The
  retry classifier is deliberately narrow — `AccessDenied` and `Forbidden` are config
  errors that no amount of reconnecting fixes, and retrying them would turn a clear
  failure into a timeout. Residual arm difference for the 失败恢复成本 column: the other
  two arms need none of this machinery.
- **Reloading data is no longer one action, because arms 1 and 2 read a different
  materialization than arm 3.** Regenerating `s3://analytics-agent-raw/parquet/` updates
  Redshift's source only; the S3 Tables Iceberg copy that Athena and DuckDB share has to
  be loaded separately, or two of the three arms silently stay a generation behind. That
  is exactly what happened on 2026-09-02, and **the table-level gate cannot see it**: the
  seed is fixed, so row counts, numeric sums, time bounds and boolean counts were
  bit-identical across all three arms while five text columns differed. Only
  `query_correctness.py --values` caught it. `scripts/lakehouse/load_parquet.py` is the
  repair path (see Useful Commands).
- `docs/architecture-v2-redshift-glue.md` and `docs/legacy.md` describe the earlier
  Redshift-only architecture and carry a banner saying so. They are history, not the
  arm-3 spec — arm 3 reuses the DDL, not the topology those docs describe.
- Aurora/Postgres is not one of the three arms; it stays a local-only convenience
  path (see `setup_local.sh` below).
- **All three arms also run on Fargate**, from one task, via `scripts/bench/fargate.py`
  (`--selftest` / `--setup` / `--build` / `--run -- <harness args>`). Not just DuckDB:
  putting all three in the same task makes region, network position and machine
  identical, so the residual difference is the engine. Correctness does not need the
  cloud (same engine, same data, same answer), but **timing does** — DuckDB is
  in-process, so measuring it on a laptop while calling Athena and Redshift across
  the network measures three different things. It adds a task definition to the
  existing `analytics-agent-relay` cluster and never touches the `ask-relay` service
  or its CloudFront stack. Task role `analytics-agent-bench-task` is read-only apart
  from two narrow prefixes (`athena-staging/` for Athena's own result mechanism,
  `bench-traces/` for the trace); neither holds data being queried, and `--selftest`
  enforces both the prefix limits and the absence of `parquet/` / `csv/`. Four things
  are easy to get wrong here and all four are pinned by `--selftest` because all four
  were hit in practice:
  1. ECS `containerOverrides.command` **replaces** the image CMD, it does not append —
     passing just `--rows` makes `--rows` the executable.
  2. IAM alone is never enough. The `s3tablescatalog` federated catalog has no
     `IAM_ALLOWED_PRINCIPALS` fallback, so every principal needs an explicit Lake
     Formation grant; a laptop works only because the developer role is a data lake
     admin. `ensure_lf_grants()` covers this, and it deliberately grants **all**
     columns of **all** tables including `user_messages` — applying the governance
     role's column exclusions would make the Athena arm read a different dataset than
     the other two while the comparison still looked sound.
  3. `s3:GetBucketLocation` does not have an `s3:prefix` condition key, so bundling it
     with a prefix-conditioned `ListBucket` makes the condition never match. Athena
     then reports `Unable to verify/create output bucket`, which points at the bucket
     rather than at the condition key.
  4. The read-action set must cover `governance.py: policy_document()`'s, which was
     itself derived empirically. Missing `glue:GetCatalog(s)` or
     `athena:GetDataCatalog` surfaces as `CATALOG_NOT_FOUND`, not as `AccessDenied`.
  Set `BENCH_TRACE_S3` for any cloud run: the container filesystem goes away with the
  task, and the trace is what locates a disagreement without re-running. The task
  definition sets it; a failed upload warns loudly but does not change the verdict.
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
- `DB_BACKEND=duckdb bash backend/run.sh` / `DB_BACKEND=redshift bash backend/run.sh`
  run the same agent on the other two comparison arms.
- `DB_BACKEND=postgres bash backend/run.sh` starts the legacy local Postgres backend.
- `backend/.venv/bin/python -m compileall -q backend` checks backend syntax.
- `backend/.venv/bin/python backend/test_agent.py "最近 7 天每天的 DAU 是多少？"`
  runs a minimal agent smoke question.
- `backend/.venv/bin/python scripts/duckdb/conn.py --selftest` checks the DuckDB arm's
  connection layer without touching the cloud; `--tables` lists what it actually
  attached to.
- `backend/.venv/bin/python scripts/duckdb/verify_load.py --rows-only` reconciles the
  35 base tables' row counts against `data/loaded_row_counts.json` (~70s, no CSV scan);
  `--columns` compares column **order** against the DDL; no argument runs the full
  metric comparison against the CSV source and needs `CSV_DIR` pointed at the 7.3G
  full-scale output. `--selftest` checks the Trino→DuckDB dialect mapping offline.
  The metric definitions come from `scripts/lakehouse/verify_load.py` — one definition
  shared by both arms, so a difference between arms is never a difference in yardstick.
- `backend/.venv/bin/python scripts/bench/correctness.py --rows` runs the three-arm
  correctness gate on row counts (~215s, needs `AWS_REGION` and
  `REDSHIFT_SECRET_ARN`); `--full` compares every metric against the CSV source
  instead; `--arm athena --arm duckdb` restricts which arms run; `--selftest`
  checks the pass/fail logic offline. Each run writes per-arm-per-table spans to
  `data/bench/correctness-<timestamp>.jsonl` including the SQL actually sent, so a
  disagreement can be located without re-running. The `elapsed_ms` in those spans
  is **not** a performance number — the gate deliberately leaves engine caches
  alone. (Concretely: two consecutive cloud `--rows` runs reported Redshift at 37.3s
  then 0.9s. Same data, same SQL — that is the result cache, and it is exactly why
  the number carries a warning.) `scripts/bench/arms.py` run directly checks that all
  three arms' probes produce identical labels and that no arm's SQL contains
  another's dialect.
- `backend/.venv/bin/python scripts/bench/query_correctness.py` runs the query-level
  gate (17 cases: 9 gates + 8 probes) on all three arms; `--gate-only` skips the probes,
  `-k join -k null` filters by key substring, `--values` runs the text-column sweep
  instead (`--values-distinct` adds `COUNT(DISTINCT)`, much more expensive on the 80M-row
  tables), `--selftest` checks the voting/invariant/tolerance logic offline. Needs
  `AWS_REGION` and `REDSHIFT_SECRET_ARN`
  (`arn:aws:secretsmanager:<region>:<account-id>:secret:redshift!<namespace>-admin_ro-<suffix>`;
  without it the Redshift arm raises instead of silently running two-armed). Writes a
  trace to `data/bench/query-correctness-<timestamp>.jsonl` with the SQL actually sent
  per arm and the first 50 rows of each result.
- `backend/.venv/bin/python eval/compare_goldens.py` compares the 27 goldens' values
  across arms. It connects to nothing — it reads the `report.dryrun.<arm>.json` products,
  so produce those first (`DB_BACKEND=<arm> python eval/run_eval.py --dry-run`, no model
  calls, no cost beyond the queries). `--arms athena redshift` picks which to compare,
  `--strict` makes the dialect and row-order notes fail too, `--selftest` checks the
  three-grade logic offline (24 assertions). Refuses to report success with fewer than
  two products present, since a one-arm cross-arm comparison is not one.
- `backend/.venv/bin/python scripts/bench/prices.py --show` prints the seven unit
  prices used by the cost column, fetched from the AWS Price List API (the `pricing`
  endpoint only exists in us-east-1, regardless of the region being priced) and cached
  with provenance to `data/bench/prices-us-west-2.json`; `--refresh` re-fetches,
  `--selftest` checks the conversions offline. Prices are never hardcoded: a dollar
  figure with no provenance cannot be audited and expires silently. Two traps are
  encoded — the API paginates (without a paginator the Redshift RPU and Fargate x86
  rates are simply absent, which looks like "no price" rather than an error) and tiered
  items expose several `priceDimensions` (taking an arbitrary one returned S3's
  >500TB tier instead of the first tier). Billing *rules* are not in the API and live
  in this file as constants: Athena's 10MB-per-query minimum, Redshift Serverless's
  60-second minimum, and Fargate having no minimum at all.
- `backend/.venv/bin/python scripts/bench/timing.py` runs the 8-query time/cost sweep
  on all three arms (`--reps N`, `-k <substr>`, `--list` to see the set and the
  execution count without spending, `--selftest` offline, `--render <trace>` to
  re-print a report from a past run for free). Four things it does that are easy to
  get wrong:
  1. **A query's timing is only reported when all three arms return identical results**
     (same `rows_key` as `query_correctness.py`), and separately **empty result sets are
     rejected** — three arms agreeing on zero rows passes the consistency gate while
     measuring nothing but metadata reads.
  2. **The client poll interval is tightened to 20ms during timing** via
     `ATHENA_POLL_INITIAL` / `REDSHIFT_POLL_INITIAL` (both now env-overridable, defaults
     unchanged at 0.15s / 0.4s). This is a measurement fix, not tuning: the poll sleep
     adds directly to wall clock and differs per arm (DuckDB is in-process and never
     polls). Measured before the fix, a Redshift query with 109ms of engine time showed
     1293ms of wall clock — roughly 70% of it our own `sleep`. The interval is **read
     back from the loaded module** and raises if it did not take effect, because setting
     the env var too late fails silently in exactly the shape of the original bug.
  3. **Noise is judged against the gap between arms, not against the median.** The
     first version warned when `(max-min)/median > 0.5` and flagged 8 of 8 queries,
     because DuckDB's median is ~250ms and a few hundred ms of jitter is >100% of that
     while Athena sits at 2300ms. The ranking was never in doubt. The check now asks
     whether the fastest arm's slowest sample still beats the runner-up's fastest.
  4. Cost is reported in **two columns** — per-query-in-isolation (each arm paying its
     own minimum) and amortized-over-the-batch (the batch's wall time charged once) —
     because Redshift's 60-second floor makes a 0.9s query and a 59s query cost the
     same. One column alone either overstates Redshift 8× or understates it.
- **Measured results (2026-09-02, 8 queries × 3 arms × 5 reps, run in both venues).**
  All 8 queries returned bit-identical results on all three arms, in both venues. The
  authoritative run is the in-region one (`timing-20260902T152410Z.jsonl`, Fargate);
  the laptop run (`timing-20260902T103231Z.jsonl`) is kept because the difference
  between them is itself the measurement.
  - **In-region steady-state medians:** DuckDB 44–229ms, Redshift 535–637ms, Athena
    1249–2311ms. DuckDB wins all 8 with non-overlapping intervals.
  - **The ranking flips depending on which segment you measure, and `timing.py` prints
    this automatically.** By engine self-report: `redshift (48–81ms) < duckdb
    (44–229ms) < athena (705–1878ms)`. By client wall clock: `duckdb < redshift <
    athena`. Redshift has the fastest *engine* of the three and the most stable one,
    but pays a flat ~0.5s Data API round trip per query (submit + poll + fetch, three
    HTTPS calls) that puts it third end-to-end. DuckDB's access path is 0ms because it
    is in-process. Engine differences are tens of ms here; access-path differences are
    hundreds. So "which arm is faster" is not answerable without saying which segment —
    and at ten times this data volume the answer could invert.
  - **Venue cost the laptop run 1.3–5.2×.** Going in-region cut Athena's and
    Redshift's per-query overhead from ~1.3s to ~0.45–0.55s (so roughly half the
    laptop overhead was WAN, half is the API itself) and sped DuckDB up 4× on small
    queries, since it reads its column chunks over the same link. DuckDB's cold-start
    ratio also dropped from 164× to 39×. `timing.py` prints a venue note and refuses to
    call laptop numbers comparable.
  - **Athena has effectively no cold start** in-region (worst case 3007ms first vs
    2311ms steady, 1×) because there is nothing to warm; Redshift's is 10× (workgroup
    resume) and DuckDB's 39× (first pull of column chunks from S3). DuckDB's steady
    state is the fastest of the three *and* it has the worst cold start — which means
    that arm's felt latency is a question about whether the Fargate task stays resident,
    not about the engine.
  - **Cold-start end-to-end, settled against the bill (2026-09-07,
    `cost-cold-20260907T053407Z.jsonl`).** One pass, 8 queries, no warm-up, no medians,
    counted from task provisioning: Athena 16.0s / $0.000498, DuckDB 36.8s (26.0s of it
    pulling the image and starting the container) / $0.004156, Redshift 39.2s / $0.144000
    billing-side. Athena is both fastest and cheapest on a cold single pass — the inverse
    of the steady-state ranking, because the thing DuckDB is fastest at (execution) is a
    tenth of what a cold pass spends. Per-arm own time sums to 65.9s against a 65.9s
    batch wall clock, which is what makes the per-arm split legitimate here.
    Athena and Redshift exclude boot deliberately: they are managed services, so the
    container is only a yardstick for comparable latency, while for DuckDB that container
    **is** the compute.
    `--settle` takes a local path (not an `s3://` URI — it now says so instead of raising
    `FileNotFoundError` on a mangled path). ECS keeps stopped tasks about an hour, so
    after that window `--task` finds nothing; `--lifecycle CREATED,STARTED,STOPPED`
    accepts the three timestamps by hand and labels them in the report as asserted rather
    than read, since nothing can cross-check them.
  - **7 of 8 queries scan under 10MB**, so Athena's per-query cost is *entirely* its
    minimum charge — the identical numbers in that column mean this data volume has not
    reached Athena's metered range, not that the queries cost the same.
  - **Batch cost (corrected 2026-09-07 — the previous figures were cross-billed).**
    Rendered from the authoritative in-region trace `timing-20260903T092605Z.jsonl`:
    Athena $0.000498, DuckDB $0.003963, Redshift $0.048000. Per-query-in-isolation
    totals: Redshift $0.384, DuckDB $0.031344, Athena $0.000498.
  - **The isolation column must include DuckDB's task boot (fixed 2026-09-07, second
    pass).** That column's premise is "this query arrives alone", and for DuckDB
    arriving alone means booting a fresh Fargate task: `PR.DUCKDB_BOOT_SECONDS` = 26.0s
    measured from the ECS `createdAt → startedAt`, plus 0.1–2.7s of execution, floored
    at Fargate's 60s per-task minimum → **$0.003918 per query, identical for all 8**
    (the charge is for the task, not the query). It had been $0.000606, understating
    DuckDB 52×. The defect shape is worth remembering: the amortized column *did*
    include boot while the isolation column did not, so one table held two different
    conventions, and the inconsistency favored DuckDB. Managed services have no boot
    line item; an in-process engine does. Post-fix isolation ranking is
    Athena $0.0000477 < DuckDB $0.003918 < Redshift $0.048 — DuckDB-to-Redshift narrows
    from 634× to 12×. Pinned by `timing.py --selftest` items 2d, 2d-1, 2d-1b, 5h-2.

    The superseded figures (Redshift $0.109, DuckDB $0.0088, Athena $0.0003) all came
    from one defect: the amortized column charged every arm for the *batch wall clock*,
    which covers 3 arms × 8 queries × (1 warm-up + 5 reps). That bills each arm for the
    other two arms' time and bills the same seconds twice, so the column summed to more
    than 100% of the run. Athena's cell was wrong the other way — its 10MB floor is
    **per query**, and summing bytes before applying one floor under-collected 7 of the
    8 floors. The column is now a pure function (`timing.amortized`) over each arm's own
    median sum, pinned by the invariant that **no arm's amount may move when another
    arm's timings move**. Fargate's 60-second per-*task* minimum is now charged too
    (`prices.FARGATE_MIN_SECONDS`); it is not charged per query, because eight queries
    sharing one task touch that floor once.
  - **Redshift's amortized cell is a lower bound, not a bill — measured 3× low.**
    Settling the cold-start trace against `sys_serverless_usage` on 2026-09-07: one pass
    of 8 queries with 37.6s of own time modelled at $0.048 but **billed $0.144** (1440
    RPU-seconds over 3 charged minutes). The 60-second minimum's unit is the *active
    minute*, not the batch: crossing a wall-clock minute boundary adds a segment, and
    the idle minute immediately after activity is charged as well (that pass touched
    05:34 and 05:35, plus 05:36 at `compute=0`). How many minutes a pass lands on is not
    derivable from a sum of durations, so `timing.py` reports the bound and says so.
    Billing-side truth comes from `cost_cold.py --settle`, which reads the usage view.
- `backend/.venv/bin/python scripts/lakehouse/load_parquet.py --probe` reloads Iceberg
  tables **from the fresh Parquet** instead of from CSV: it registers a Glue external
  table over `s3://analytics-agent-raw/parquet/<table>/` and `INSERT`s across catalogs.
  `--probe` compares every column's aggregate on both sides and changes nothing;
  `--apply` does `DELETE FROM` + `INSERT` (**not** `load.py --recreate`'s DROP/CREATE, so
  Lake Formation grants survive); `--selftest` checks type mapping, SQL shape, batching
  and the diff classifier offline. It defaults to the three tables that were stale
  (`orders`, `push_notifications`, `user_attributions`) rather than to all 35 — a
  mis-fire on "all tables" would `DELETE` 32 correct tables. Three things it encodes
  because all three were hit: Athena Iceberg `INSERT` allows at most 100 open partition
  writers so `orders` (`day(placed_at)`, 91 days) is batched by month; the Hive external
  table's `timestamp` is **millisecond** semantics while the Iceberg target is
  `timestamp(6)`, so the comparison normalizes instants rather than renderings (`.000`
  ⟷ `.000000` would otherwise be 32 false diffs, while real truncation `.535` ⟷ `.53532`
  still fails); and whether milliseconds are lossless here cannot be checked with Athena
  (it would report 0 either way) — it was established with a local DuckDB `read_parquet`
  census, which `--selftest`'s precondition documents. It scans ~240 MB per `--probe`.
- `backend/.venv/bin/python scripts/bench/fargate.py --selftest` audits the task
  role's policy, the Lake Formation grant plan and the image's compute knobs offline;
  `--setup` creates/updates ECR, both roles, the LF grants and the task definition
  (idempotent, safe to re-run); `--build` needs docker and pushes `linux/amd64`;
  `--run -- --rows` runs the gate on Fargate and streams CloudWatch logs until the
  task stops, exiting with the container's exit code.
- `bash scripts/test_all.sh` runs the full local test suite (L0–L6).
- `bash scripts/test_all.sh --l0` runs only L0 — no AWS calls, no credentials, and it
  exits 0 when L0 is green (the full run exits 1 at the AWS-identity gate without
  credentials, so its exit code cannot express "L0 passed"). This is what CI runs
  (`.github/workflows/offline.yml`: `--l0` + `negative_tests.py --offline` + the CDK
  policy assertions). CI covers **nothing** above L0 — see `docs/test-plan.md`.
- `bash scripts/test_all.sh --l8` appends the fault-injection negative tests
  (`scripts/negative_tests.py`, 55 cases): it temporarily edits repo files and restores
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
  the only thing covering `backend/agent.py`. ("L7" is the *tier* name from
  `scripts/test_all.sh`; inside `cases.json` the 27 cases are levels 1–5, so
  `run_eval.py --level 7` matches nothing.)
- **The mart layer has to be built per arm, and nothing except the goldens notices if
  it isn't.** The 13 computed tables (`mart_*` ×4, `dwd_*`/`dws_*`/`fin_*`/`growth_*`
  ×6, `orders_backup_20251201`, `tmp_campaign_roi_analysis`, `meta_snapshot`) are not
  loaded from CSV — each arm computes them from its own base tables:
  - **athena / duckdb**: `database/iceberg/02_mart.sql`, applied once into the S3 Tables
    namespace. DuckDB attaches that same namespace, so it gets the layer for free — one
    build covers two arms.
  - **redshift**: `database/redshift/02_mart.sql` + `03_derived.sql`, via
    `scripts/redshift/rsql.py --file`. `load_from_s3.py` loads the **35 base tables
    only** and does not run these, so a freshly loaded Redshift arm has no mart layer.
    That is how it was actually found: 11 of 27 goldens returned
    `relation "mart_daily_kpi" does not exist`. Applying both files takes ~40s and
    fixed all 11.

  Why no gate caught it: `scripts/bench/correctness.py` takes its table list from
  `gen_ddl.parse_source()`, which globs `database/0[1-8]_*.sql` — the base tables. The
  mart layer is invisible to it, and has no CSV upstream to be a baseline anyway. The
  gate that does cover it is `eval/compare_goldens.py`, where a missing table shows up
  as `unavailable`. Run that after any reload.
- **`database/redshift/02_mart.sql` carries a known latent drift, deliberately.** Its
  `mart_daily_kpi` refund predicate is `WHERE refunded_at IS NOT NULL`, missing the
  `status = 'refunded'` filter that `database/09_mart.sql:67` and the Iceberg version
  both have. On this data every row with a non-null `refunded_at` is already
  `status='refunded'`, so both arms compute `refund_amt` = 9,897,495.82 — verified
  identical. The drift is `scripts/lakehouse/verify_mart_parity.py`'s test fixture
  (`--against database/redshift/02_mart.sql` is what demonstrates the script works), so
  do not "fix" it without replacing the fixture. It becomes live the day partial refunds
  write `refunded_at` without `status='refunded'`, and no test would go red — so if the
  generator's refund logic changes, re-check this one first.
- **Cross-arm golden values: `eval/compare_goldens.py`.** Reads the per-arm
  `--dry-run` products offline and compares the 27 goldens' *values*, which is a
  different question from the one `--dry-run` answers ("does the SQL execute here").
  Three grades, because the first version had one and flagged 5 differences of which 4
  were not defects:
  - `value` — beyond the case's `tolerance_pct`. Gate; non-zero exit.
  - `dialect` — differs but within tolerance. The one measured cause is **decimal
    division scale**, and all three arms pick a different one. `sum(gmv_attributed) /
    sum(cost)` over two `decimal(14,2)` columns:

    | arm | value | scale kept |
    |---|---|---|
    | athena (Trino) | 68.31 | 2 dp — Trino derives a fixed result scale |
    | redshift | 68.3051 | 4 dp |
    | duckdb | 68.30515774416658 | full double |

    Largest relative spread 0.0075% against a 0.5% judging tolerance, so all three score
    correct. Hits `L4-roi-cac-by-channel` and `L3-cac-overall` — both are ratios of two
    sums, which is the shape that triggers it. A finding for the write-up, not a bug; but
    it does mean any *new* case whose tolerance is tighter than ~0.01% will fail on the
    arm-vs-arm comparison for reasons that have nothing to do with the data.
  - `order` — row order differs, multiset identical. Those goldens have no `ORDER BY`,
    so row order is not defined by the query and any order is correct; the three cases
    where it happens (`L2-gender-dist`, `L2-device-dist`, `L2-order-status-dist`) all
    have `judge.mode: "set"` anyway. Compared as multisets, noted separately.

  `--strict` promotes all three to gates. **Current status: all three arms 27/27**, with
  the 2 dialect and 4 order notes above and nothing in the `value` grade — so the three
  architectures compute the same answers on all 27 cases, and time and cost are therefore
  worth comparing.
- **L7 results, all three arms (27 cases each, `global.anthropic.claude-opus-4-8`).**

  | arm | pass | avg s/case | docs | SQL | date |
  |---|---|---|---|---|---|
  | athena | 27/27 | 43.2 | 2.2 | 0.8 | 2026-09-01 |
  | redshift | 27/27 | 47.9 | 2.5 | 0.8 | 2026-09-03 |
  | duckdb | 27/27 | 50.5 | 2.4 | 0.9 | 2026-09-03 |

  All three at 100%, all five levels. **Do not read `avg_s` as a performance number**:
  these runs were from the laptop, the Redshift one ran concurrently with a DuckDB WAN
  load, and the figure is dominated by model latency (~30–50s/case) not by the engine
  (tens of ms). The authoritative time and cost numbers are `timing.py`'s in-region ones
  below. What this table does say is that the arm makes no difference to answer quality,
  which is the precondition for comparing time and cost at all.
- **Running L7 on a non-Athena arm: three things that bite.** The arm is selected with
  `DB_BACKEND`, and the cases, goldens and judges are all shared — the arm is the only
  variable, which is what makes the comparison meaningful.
  1. **Report paths used to be hardcoded**, so running `DB_BACKEND=duckdb` silently
     overwrote `eval/report.json` — the Athena baseline, which is the only historical
     reference L7 has. Now the filename carries the arm (`report.duckdb.json`,
     `report.redshift.json`; Athena stays at `report.json` so the baseline path is
     unchanged) and `meta.arm` plus the report header record which arm produced it.
     A report that does not say which arm it measured is unusable here.
     `--dry-run` products are tagged the same way (`report.dryrun.<arm>.json`), for a
     different reason: `compare_goldens.py` needs two or more of them present at once,
     and the old fixed path meant running two arms back to back left only the second.
  2. **`db.py` gives DuckDB a 4× tighter statement timeout than the other two arms**:
     `STMT_TIMEOUT_MS` (default 15s) is multiplied by 4 for Athena and Redshift and
     used as-is for DuckDB. In-region that is irrelevant (DuckDB's queries run in
     44–229ms), but from the laptop DuckDB's *first* touch of each table costs 5–13s
     over WAN, so goldens start failing at case 8 with `golden_error` — which reads
     like a dialect problem and is actually a timeout. The same SQL runs fine
     standalone. The asymmetry itself is left alone: a tighter timeout is the safe
     direction, and L7 measures the agent's SQL-writing quality, which does not depend
     on the venue.

     **Use `SQL_TIMEOUT_MS=2400000` for laptop DuckDB runs, not 120000.** 120s gets
     through the L1 cases and then stalls on `L2-top-pages-7d`, whose golden
     double-full-scans the 12.9M-row `page_views` over WAN. At 2400000 the whole thing
     completes: goldens 27/27 in ~45 min, full L7 27/27 in ~50 min at 50.5s/case. So
     the earlier conclusion that DuckDB's L7 needs to be in-region was wrong — it needs
     a timeout that fits the venue. In-region is still the right venue for *timing*,
     because a WAN-bound number is not an engine number.
  3. `_ADAPTERS` in `run_eval.py` maps a backend to its dialect converter and has
     entries only for `redshift` and `athena`. DuckDB gets the goldens as raw
     Postgres, which works because DuckDB accepts `::` casts, `interval '6 days'` and
     `FILTER (WHERE ...)` — the absence of an entry is correct, not an omission.
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
