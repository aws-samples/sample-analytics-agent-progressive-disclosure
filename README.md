**English** | [中文](README.zh-CN.md)

# Analytics Agent · Progressive Disclosure

[![License: MIT-0](https://img.shields.io/badge/License-MIT--0-blue.svg)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/Python-3.11-3776AB.svg?logo=python&logoColor=white)](backend/requirements.txt)
[![Claude Agent SDK](https://img.shields.io/badge/Claude_Agent_SDK-Opus_4.8-cc785c.svg?logo=anthropic&logoColor=white)](https://docs.anthropic.com)
[![Amazon Bedrock](https://img.shields.io/badge/Amazon_Bedrock-FF9900.svg?logo=amazonaws&logoColor=white)](https://aws.amazon.com/bedrock/)

An "ask-your-data" demo: ask in plain language, and the agent locates the right tables, writes correct SQL, runs it, and returns a chart plus a conclusion.

What it really sets out to prove is one thing: **turning a database schema into a "data dictionary" — a markdown doc tree the agent browses on demand, reading routes layer by layer before writing SQL — is more accurate and cheaper than stuffing the entire schema into the context window, or exploring the database from scratch on every question.** That mechanism is exactly the *progressive disclosure* idea behind Agent Skills.

The dataset is deliberately messy: a content + commerce app (think a social-shopping platform), with 35 raw tables across 8 business domains — plus a derived layer (dwd/dws/ads), a governed mart, and a couple of deliberately planted trap tables, **48 tables in the Glue Data Catalog, ~220k rows**, stored as **Apache Iceberg tables in an S3 table bucket** and queried with **Amazon Athena**. The more tables there are, the more definitional traps appear (is GMV gross or net of refunds? is a coupon redemption recorded on the template table or the claim table? should an A/B test be time-boxed?) — and that's precisely where "read the right doc first, then write SQL" earns its keep.

![Analytics Agent architecture — animated](docs/architecture.svg)

> The diagram animates in the rendered README (GitHub embeds the SVG as an image). Blue = request in-flight, teal = the streamed reply, green = the knowledge tree synced from S3 at cold start, sky = `run_sql` hitting Athena over the S3 table bucket (HTTPS + IAM — no VPC connection, no connection pool, no password in the container). The `/ask` hop rides a CloudFront VPC origin to an internal ALB and a Fargate relay, which verifies the Cognito JWT and streams SSE from the AgentCore Runtime.

## What's new in v3 (S3 Tables + Athena)

v1 kept the data in a local/Aurora PostgreSQL (35 tables, ~190k rows) and the metadata in a hand-written markdown tree. v2 moved the data to Redshift Serverless and introduced the Glue Data Catalog. **v3 retires Redshift entirely**: the table bucket *is* the Iceberg catalog, so once it's federated into Glue, the namespace maps straight to a Glue database and the whole datashare layer disappears.

- **Data**: **S3 Tables (Apache Iceberg) + Amazon Athena**. Queries are Athena API calls (HTTPS + IAM) — no VPC connection, no connection pool, no password in the container, and nothing to keep warm. Setup and loading are `scripts/lakehouse/setup.py` → `gen_ddl.py` → `load.py`.
- **Metadata**: split into *declared* (`schema_manifest.yaml` + DDL, in git) / *actual* (**Glue Data Catalog**, generated, never hand-edited) / *semantic* (`knowledge/` table cards — when to use, caveats, metric definitions), with three-way reconciliation (`scripts/lakehouse/reconcile.py`). On first run it caught 5 real doc drifts.
- **Reconciling the *values*, not just the columns**: `scripts/lakehouse/verify_enums.py` diffs the enum tables in the knowledge cards against the values actually present in each column, in both directions. Column-level reconciliation cannot see this class of drift: a card saying `status='active'` when the data says `'on_sale'` passes every syntax and catalog check and returns an **empty result set**, which reads as "there are no active items". It found **46 drifts** on first run — including six event names in `events.event_name` that do not exist at all, which would make every stage of a funnel query return 0.
- **The UI reads the catalog**: `GET /api/catalog` assembles Glue + `information_schema` + `knowledge/` + manifest into what the frontend renders, replacing numbers hard-coded in HTML.
- **Metrics as function calls**: the governed definitions in `metrics/governed_metrics.md` are compiled from a registry (`backend/metrics_def.py` + `metric_layer.py`) and called via a `call_metric` tool, so GMV / CAC / ROI / refunds return one authoritative number instead of being re-derived per question.
- **Eval harness**: 27 golden-SQL cases pass 27/27 on Athena (`eval/`, 2026-08-23 — the first full run after the funnel and retention semantics fixes); golden SQL stays in Postgres dialect and is rewritten at runtime (`scripts/gen/pg_to_trino.py`).
- **Two costs of the move, stated plainly**: (1) the SQL dialect is now **Trino**, so `::` casts, `date + 7`, `DISTINCT ON` and `interval '30 days'` are all invalid — the prompts, metric SQL and knowledge examples were rewritten (`interval '30' day`); (2) **the governance layer changed primitive**: Redshift dynamic masking (column present, value masked) has no Lake Formation equivalent, so it became **column-level exclusion** (those columns are simply not in the grant) — see [Security](#security).

What is *not* in v3: the 80M-row generator path (`scripts/gen/` → Parquet → COPY), and **value-level masking** — Lake Formation has no such primitive; that one was a Redshift feature. `scripts/lakehouse/load.py` loads the committed CSV seed data (~190k rows in the base tables). Note that this says what the repository ships, not what any particular lake currently holds — the development account's lake has since been reloaded at 80M rows, and `load.py` would overwrite it, so check the row count before reloading ([docs/deployment.md](docs/deployment.md#数据说明)). v2's design and migration pitfalls are archived in [docs/architecture-v2-redshift-glue.md](docs/architecture-v2-redshift-glue.md); the v1 PostgreSQL path (local Docker container) still runs but is **legacy — kept, not maintained**; see [docs/legacy.md](docs/legacy.md).

## What it looks like

The left rail has 6 example questions (easy to hard: 30-day GMV → subscription plans → conversion funnel → coupon redemption → channel CAC → A/B experiment). As the agent works, it streams "which doc am I reading right now" in real time, so you can watch progressive disclosure happen. The UI has an **EN / 中 language toggle** (top-right). The brain is an agent built on the **Claude Agent SDK**, running on **Amazon Bedrock** (default `global.anthropic.claude-opus-4-8`, cross-region inference via the `global.` prefix) — it does not depend on the Claude Code CLI. End to end, a question takes ~25–70s (Opus reads several docs and makes several tool round-trips). Note: the UI *chrome* toggles between English and Chinese, but the **analysis content** (insights, generated SQL, results) comes back in Chinese, because the knowledge base the agent reads is Chinese.

To run it locally see [Run locally](#run-locally); to deploy it into your own AWS account see [docs/deployment.md](docs/deployment.md).

> The README and this project's docs are English; the **knowledge base under `knowledge/` is kept in Chinese** (it is the agent's source of truth and matches the Chinese sample data). A full Chinese README is at [README.zh-CN.md](README.zh-CN.md).

## Three layers, one idea

The project is the same idea realized at three levels:

```
┌──────────────────────────────────────────────────────────────┐
│  3) Web App (the product)      backend/ + web/                │
│     Claude Agent SDK + Bedrock + FastAPI, with 5 MCP tools:   │
│     read_doc / run_sql / call_metric / compute_stats /        │
│     present_result — the frontend streams each step over SSE  │
├──────────────────────────────────────────────────────────────┤
│  2) Knowledge base (data dictionary + routing)   knowledge/   │
│     a markdown doc tree:  domains/_index.md   (L1 routing)    │
│       → domains/<domain>/_index.md            (L2 pick table) │
│       → domains/<domain>/<table>.md           (L3 fields)     │
│     + metrics/ (metric definitions) + relationships.md        │
├──────────────────────────────────────────────────────────────┤
│  1) Data layer (a reproducible lakehouse)  database/ + scripts/│
│     S3 Tables (Iceberg) + Athena — 48 tables in the Glue Data  │
│     Catalog, ~220k rows (toolchain: scripts/lakehouse/)        │
│     (v1 local PostgreSQL path kept as legacy)                  │
└──────────────────────────────────────────────────────────────┘
```

- **Data layer** is the foundation: `database/iceberg/` holds the v3 DDL (35 base tables + 13 mart/derived/trap tables), *generated* from the v1 DDL by `scripts/lakehouse/gen_ddl.py --check` so it cannot silently drift from its source; `scripts/lakehouse/` then creates the table bucket and namespace, federates it into Glue, loads the CSV seed data, and reconciles catalog ⟷ manifest ⟷ knowledge base.
- **Knowledge base** is the core asset: the markdown doc tree under `knowledge/` *is* the data dictionary. The web app's `read_doc` tool reads it route by route, it is baked into the image, and it is the agent's only source of knowledge.
- **Web App** productizes all of it: a web tool anyone can open, visualizing the whole "read docs → write SQL → render chart" flow.

## Knowledge base (data dictionary)

`knowledge/` is the agent's single knowledge base — a markdown doc tree covering all 8 domains plus the governance (mart) layer:

- `domains/`: three-level routing (`_index.md` master index → per-domain index → per-table card). Table structure, column enums, and example SQL live here.
- `metrics/`: metric definitions. `governed_metrics.md` is the official governed definition, mapped one-to-one to the `call_metric` tool in `backend/metrics_def.py` (code is the source of truth for the definition).
- `analysis/`: SOPs for 5 in-depth analysis methods, with statistical formulas.
- `relationships.md`: join keys across tables.

See [knowledge/README.md](knowledge/README.md) for details. **The knowledge doc tree is kept in Chinese.**

> Early on this tree lived under `.claude/skills/` as a Claude Code CLI skill (there were even "shortcut-template" and "pure-routing" variants for comparison). After productization it was consolidated into a top-level `knowledge/`, and the CLI-skill variant was removed.

## Two paradigms: text-to-ETL and text-to-insight

The same data is modeled in two layers, so one demo tells two stories:

- **Raw data · querying (text-to-ETL)**: 35 raw detail tables. For fetch-style questions ("30-day GMV", "conversion funnel", "channel CAC"), the agent joins multiple tables on the fly, decides the definition itself, and writes complex SQL. The challenge is constructing the logic correctly and avoiding definitional traps.
- **Governance layer · insight (text-to-insight)**: on top of the raw tables, `database/iceberg/02_mart.sql` builds 4 pre-aggregated "governed" tables (`mart_*`), where GMV / new users / attribution / repurchase definitions are **frozen** into the tables and into `metrics/governed_metrics.md`. For judgment-style questions ("review monthly GMV — what drove it?", "how's the business this week?", "repurchase rate"), the agent writes simple SELECTs against clean tables and spends its effort on slicing, attribution, and drawing conclusions.

This mirrors two real-world situations: before governance, AI turns messy data into the right numbers; after governance, the plumbing is done and AI helps you analyze, attribute, and judge. The left-rail presets are split into these two groups. How the agent chooses between the layers lives in the system prompt in `backend/agent.py` (fetching goes to raw domains; diagnosis / review / holistic judgment goes to the governance layer).

## Prerequisites

- **AWS account with Amazon Bedrock model access.** In the Bedrock console, request access to the model this sample uses (default: Claude Opus 4.8) in your Region. The sample calls it via the cross-region inference profile (`global.anthropic.claude-opus-4-8`).
- **AWS credentials** on the standard chain (`~/.aws`, environment variables, or an EC2 instance role). The default (`athena`) backend authenticates to Athena and Glue with IAM — no database password on your machine.
- **An S3 table bucket loaded with the sample data, federated into Glue, plus an Athena workgroup** — DDL in `database/iceberg/`, one-time setup and loading in `scripts/lakehouse/` (`setup.py` creates the table bucket `analytics-agent-tables`, the namespace `app_analytics` and **two** workgroups — `analytics-agent-wg` for admin-side work and `analytics-agent-ro-wg` for the agent's least-privilege role; `load.py` loads the CSVs). The two are deliberately separate: a workgroup's `OutputLocation` decides where the **plaintext result CSV** lands, so sharing one prefix would let the agent role read Lake-Formation-excluded columns back out of admin-side result files. Athena bills per byte scanned, so there is nothing to leave running idle — but the query result location is a normal S3 bucket you should include in your cleanup.
- **Python 3.11** — for the backend.
- **Node.js 20+ and the Claude Code CLI** — install with `npm install -g @anthropic-ai/claude-code`. The Claude Agent SDK launches the `claude` CLI as a subprocess, so it must be on your `PATH`. (The Docker image installs this for you; the local `run.sh` path needs it on your machine.)
- *(Legacy v1 path only)* **Docker + Docker Compose** and/or **PostgreSQL 16** — for the local-container database (`DB_BACKEND=postgres`, see [docs/legacy.md](docs/legacy.md)).

> **Cost**: this is not free to run. Each question invokes Claude Opus on Amazon Bedrock (tens of seconds of reasoning plus several tool round-trips) and scans a few Athena bytes; the cloud deployment additionally runs an AgentCore Runtime, a Fargate relay + ALB and a CloudFront distribution — you pay standard Bedrock token and infrastructure charges. Tear the cloud resources down when you're done (see [Cleanup](#cleanup)).

## Run locally

The backend picks its data backend from `DB_BACKEND`: **`athena` (default)** queries the S3 table bucket through Athena; `postgres` is the legacy v1 local container.

**Default path (Athena):**

```bash
# Prereqs: a Python venv, usable AWS credentials (aws sso login / AWS_PROFILE),
# Bedrock model access, and a loaded S3 table bucket (see scripts/lakehouse/)
cd backend
./run.sh                            # checks credentials, then starts uvicorn (8000)
# open http://127.0.0.1:8000/
```

`run.sh` derives the AWS account from `aws sts get-caller-identity` at startup — nothing account-specific lives in the repo. To override resource names (workgroup, catalog, namespace, region), copy `.env.local.example` to `.env.local`; note that **command-line environment variables win over that file**, so `AWS_REGION=… ./run.sh` does what it looks like it does. To poke at the lakehouse manually, use `scripts/lakehouse/athena.py "SELECT ..."`.

**Legacy path (v1 local Postgres, ~190k rows, not maintained):** `docker compose up -d` starts a database-only container (35 tables, auto-loaded CSVs), and `docker-compose.cloud.yml` runs the full v1 two-container stack with `DB_BACKEND=postgres` pinned. Details and the exact boundary of what is still maintained: [docs/legacy.md](docs/legacy.md).

Backend architecture, environment variables, and self-test commands are in [backend/README.md](backend/README.md).
If the backend is unreachable, `web/index.html` automatically falls back to an offline demo (built-in mock data, frozen at v1) — you can even open the file directly to see the UI. The full test suite is `bash scripts/test_all.sh` (layers L0–L6: reconciliation, consistency, eval, endpoint and render contracts; add `--l8` for 32 fault-injection negative tests). What each layer means, what a failure tells you, and — importantly — **what has no automated coverage at all**: [docs/test-plan.md](docs/test-plan.md).

## Project structure

```
sample-analytics-agent-progressive-disclosure/
├── README.md                    # this file (English)
├── README.zh-CN.md              # Chinese README
├── PROJECT_STATUS.md            # evolution log (CLI Skill → Web App → EC2 → AgentCore → Redshift+Glue → lakehouse)
├── docs/
│   ├── architecture-v2-redshift-glue.md  # v2 architecture (superseded, kept as the migration record)
│   ├── legacy.md                 # exactly which code is kept-but-not-maintained
│   ├── test-plan.md              # current acceptance layers L0–L8 + the "no automated coverage" list
│   ├── test-plan-v2.md           # v2 acceptance flow (superseded, kept for the method)
│   └── deployment.md             # deployment guide
├── schema_manifest.yaml         # declared state of the derived layer (single source of truth)
│
├── database/                    # ① data layer · DDL
│   ├── iceberg/                  #   v3 DDL: 01_tables (35 base) / 02_mart (mart + derived + traps)
│   ├── redshift/                 #   v2 DDL (retired with Redshift, kept as record)
│   └── 0*_*.sql · 09_mart.sql    #   v1 PostgreSQL DDL — the source gen_ddl.py generates from
├── data/csv/                    # ① seed data (35 CSVs) — what the lakehouse actually loads
├── scripts/                     # ① generation / loading / verification toolchain
│   ├── lakehouse/                #   v3 toolchain: setup / gen_ddl --check / load / reconcile / verify_*
│   ├── gen/                      #   v2 generator → Parquet + the dialect rewriters (pg_to_trino.py)
│   ├── redshift/ · glue/         #   v2 Data API client + Glue registration (retired with Redshift)
│   ├── audit/ · consistency/     #   data quality audit + migration-loss snapshot
│   ├── deploy/                   #   build_catalog_json.py + deploy_web.sh (frontend + metadata snapshot)
│   │                             #   sync_agent_code.py: regenerates analyticsagent/ shared code from backend/
│   ├── ui/                       #   render contract tests (no browser needed)
│   ├── negative_tests.py         #   L8: inject a defect, assert the checker goes red, restore (38 cases)
│   └── test_all.sh               #   the whole gate: L0–L6 (--l8 appends the negative tests)
│
├── knowledge/                   # ② knowledge base · data-dictionary doc tree (read by read_doc; kept in Chinese)
│   ├── README.md                 #   knowledge-base notes + single-source-of-truth rules
│   ├── domains/_index.md         #   3 levels: master index → domain index → table card
│   ├── domains/<domain>/<table>.md
│   ├── domains/mart/             #   governance layer: 4 table cards (text-to-insight)
│   ├── metrics/                  #   metric definitions (incl. governed_metrics.md)
│   ├── analysis/                 #   5 in-depth analysis SOPs + formulas
│   └── relationships.md          #   cross-table relationships
│
├── eval/                        # ② eval harness: 27 golden-SQL cases + baselines (run_eval.py)
│
├── backend/                     # ③ Web App · the brain (Agent SDK + Bedrock)
│   ├── agent.py                  #   system prompt + event-stream parsing
│   ├── tools.py                  #   MCP tools: read_doc/run_sql/call_metric/compute_stats/present_result
│   ├── metrics_def.py · metric_layer.py · stats.py  # metrics-as-code + stats compute
│   ├── db.py                     #   read-only SQL safety boundary (DB_BACKEND: athena | postgres)
│   ├── catalog.py                #   /api/catalog: Glue + information_schema + knowledge/ + manifest → UI metadata
│   ├── server.py                 #   FastAPI + SSE, serves the frontend
│   └── run.sh · Dockerfile · requirements.txt
├── web/index.html               # ③ Web App · frontend (progressive-disclosure UI, EN/中 toggle)
│
├── docker-compose.yml           # legacy v1: database container only
└── docker-compose.cloud.yml     # legacy v1: database + FastAPI app (two containers)
```

## Data overview

| Domain | Tables | Representative tables |
|--------|--------|-----------------------|
| User | 5 | users, user_profiles, user_devices, user_segments, user_segment_members |
| Behavior | 4 | events, sessions, page_views, event_definitions |
| Transaction | 4 | orders, order_items, payments, subscriptions |
| Product | 3 | products, categories, product_tags |
| Social | 6 | posts, post_likes, post_comments, post_shares, user_follows, user_messages |
| Marketing | 5 | campaigns, coupons, user_coupons, banners, push_notifications |
| Attribution | 5 | channels, ad_campaigns, ad_creatives, channel_daily_costs, user_attributions |
| Experiment | 3 | ab_tests, ab_test_variants, ab_test_assignments |

**35 base tables, ~190k rows** (largest: `post_likes` 35.7k, `page_views` 30.2k, `user_coupons` 22.7k, `events` 20k). The Glue Data Catalog holds **48 tables / ~220k rows** in total once you add the governed mart (4 `mart_*` tables, definitions frozen — see [Two paradigms](#two-paradigms-text-to-etl-and-text-to-insight)), a derived layer (dwd/dws/ads/fin), a `meta_snapshot` anchor table, and two deliberately planted trap tables (`orders_backup_20251201`, `tmp_campaign_roi_analysis` — documented in the knowledge base as ⛔ deprecated, to test whether the agent gets misled by stale copies).

Every business ratio in the seed data — events per user, orders per user, the attribution coverage gap — is deliberate, and the definitional traps described in `knowledge/` depend on them. v1's full schema record: [database/00_schema_overview.md](database/00_schema_overview.md).

> **Note on time**: this is a static sample, and **its time axes do not all end on the same day** — that asymmetry is itself one of the traps.
>
> | Table | Axis ends | |
> |---|---|---|
> | `orders` / `mart_daily_kpi` / `events` | 2026-01-24 | the real business "today" |
> | `fin_daily_revenue` | 2026-02-02 | refunds keep landing 9 days past the last order |
> | `channel_daily_costs` / `mart_channel_daily` | 2026-09-01 | ad spend was loaded ~8 months ahead of any attribution |
>
> So **anchor "today" to `(SELECT max(as_of_date) FROM meta_snapshot)` = 2026-01-24**, one business calendar for every metric — *not* to each table's own `max()`, and never to `current_date` / `now()` (those land outside the data and return empty). Using a per-table `max()` doesn't error; it silently answers a different question — measured example: it made "which channel has the lowest CAC?" name a channel that spent nothing inside the business calendar. The metric layer (`backend/metrics_def.py` + `metric_layer.py`) enforces the single anchor, so anything going through `call_metric` is safe; **the system prompt in `backend/agent.py` still teaches the model to use each table's own `max()` — exactly the path that produced the wrong answer above, and an open gap**. Bounded windows carry an upper bound (`dt > anchor - interval '30' day AND dt <= anchor`) for the same reason.

## What you can ask

User analysis (DAU/MAU, retention, profiles, segments), transaction analysis (GMV, average order value, conversion funnel, subscriptions), product analysis (sales ranking, category mix), social analysis (engagement, KOLs, follow graph), marketing analysis (campaign performance, coupon redemption, push), channel analysis (attribution, CAC, ROI), and experiment analysis (A/B variant comparison).

A difficulty-graded list of questions is in [test_questions.md](test_questions.md).

## Security

- **Read-only SQL boundary.** Every generated query passes through `backend/db.py`, which enforces a single read-only `SELECT`/`WITH` statement (forbidden-keyword guard, statement timeout, row cap). The boundary is backend-agnostic — switching `DB_BACKEND` does not weaken it.
- **Data-layer governance: a least-privilege role plus column-level exclusion, enforced at query time.** The agent does not query with the backend process's own credentials — it assumes a dedicated role (`analytics-agent-ro`) and **Lake Formation** decides what that role may read: `user_messages` (private message bodies) is not granted at all, and `users.email` / `users.phone` / `user_profiles.birth_date` are excluded from the grant. Both the setup and the verification live in `scripts/lakehouse/governance.py` (`--apply` / `--verify` / `--verify-backend`); the backend wires it up through `AGENT_ROLE_ARN` and `/health` reports the effective `identity`.
  - **One honest capability regression.** v2 used Redshift dynamic data masking (the column is there, the value becomes `***@masked.invalid`); **Lake Formation has no value-masking primitive**. So the v3 equivalent is "the column is not in the grant at all" — it is absent from `SELECT *`, and naming it explicitly returns `COLUMN_NOT_FOUND`. The trade-off, and why Glue Catalog Views were not worth the second unchecked metadata surface, are written up in that script's docstring rather than quietly swapped.
  - **The governance layer is itself covered by negative tests**, not merely "configured": three `gov-*` cases inject a loosened policy, blind probes (re-running them as the caller's admin identity **must** go all red — all green means those assertions verified nothing), and a role that exists with grants issued while `db.py` still queries as admin. The last one is the most realistic regression: `/health` still looks right.
  - With `AGENT_ROLE_ARN` unset (the local-development default) the backend uses your own credentials, usually an admin — **there is no column-level boundary in that mode**, only the read-only guard above. Read the local demo with that in mind.
- **The two boundaries do not cover for each other.** The read-only guard governs "may not write"; the governance layer governs "may not read". The agent does hold SELECT on the granted tables, so the read-only guard cannot be dropped — and conversely that guard would happily pass `SELECT email`. Each has its own self-test and its own inverted negative test (`readonly-guard-hole` / `gov-*`); before those existed, loosening either would not have turned anything red.
- **No inbound database port.** There is no database endpoint at all: queries are Athena API calls (HTTPS + IAM) against an S3 table bucket. No password exists to store — the agent runtime uses IAM temporary credentials, and the read-only IAM policy is the outer bound on what those credentials can do.
- **Untrusted model output in the UI.** Model-generated text, SQL result column names, and DB cell values are all treated as untrusted and HTML-escaped (`esc()` in `web/index.html`) before reaching `innerHTML`. Column names matter here: a question can steer the agent into `SELECT 1 AS "<img src=x onerror=…>"`, so headers are escaped exactly like cells.
- **Optional app-layer auth.** The cloud deployment puts Amazon Cognito in front (SRP login, JWKS verification); local development runs open by default (`AUTH_ENABLED` unset).
- **Static-analysis suppressions.** A small number of known false positives are suppressed inline (`# nosec` / `# nosemgrep`): the data generators use `random` for demo data (non-cryptographic), and the metric compiler builds SQL from a trusted registry (filter values are escaped). Both are reviewed false positives.

To report a security issue, follow the guidance in [CONTRIBUTING.md](CONTRIBUTING.md#security-issue-notifications) — please do **not** open a public GitHub issue.

## Cleanup

The default path runs against AWS resources you created, so tear them down when finished: delete the **S3 table bucket** (`analytics-agent-tables`, which deletes the Iceberg tables with it), the **Glue federated catalog entry** that points at it, **both Athena workgroups** (`analytics-agent-wg` and `analytics-agent-ro-wg`) and their **query-result staging bucket** (`athena-staging/` including the `agent/` sub-prefix), plus the governance layer's **IAM role** (`analytics-agent-ro`) and the **Lake Formation grants** issued to it. Athena has no idle cost — it bills per byte scanned — so the recurring charge here is S3 storage, not compute. For the **cloud web deployment**, additionally tear down the Fargate relay + ALB, the Cognito user pool, the site S3 bucket, and disable & delete the CloudFront distribution.

If you ran `agentcore deploy`, that adds its own set: the `AgentCore-analyticsagent-default` stack, the `analytics-agent/runtime` secret, the `analytics-agent-knowledge` bucket (**versioned — you must delete every version before the bucket will go**), the `lakehouse-runtime-access` inline policy on the execution role, and the execution-role principal added to `analytics-agent-ro`'s trust policy (**edit it, don't overwrite — your own developer principal is in there too**). CDK bootstrap's resources (`CDKToolkit` stack, assets bucket, container-assets ECR repo, 5 roles) are **account-shared**; leave them if anything else in the account uses CDK. Steps are in [docs/deployment.md](docs/deployment.md).

## License

This project is licensed under the MIT-0 (MIT No Attribution) License — see [LICENSE](LICENSE).
