# AGENTS.md

You are (probably) an AI agent that just landed here. This file tells you
what this project contains and how to get productive in ≤30 seconds.

## What this is

An **idea discovery database** for SaaS and service micro-business ideas.
It aggregates ~26,500 startup ideas from accelerator cohorts
(Y Combinator, Sequoia Arc, a16z Speedrun), VC portfolios (General
Catalyst, Lux Capital, Playground Global) and micro-business
communities (Show HN, Reddit r/SideProject, r/indiehackers,
r/microsaas, r/buildinpublic), then:

1. **Clusters** them with HDBSCAN over MiniLM embeddings (1,593 clusters
   incl. sub-clusters).
2. **Tags** each row via Claude Haiku 4.5 on two orthogonal axes —
   `mechanism_tags` (what it *does*) × `sector_tags` (who/where it
   applies), plus launch-assessment flags (`solo_buildable`,
   `ai_first_advantage`, `moat_type`, `niche_specificity`).
3. **Scores** each extracted row 0-100 on how launch-feasible it looks
   (`opportunity_score`).

The goal is gap-finding across a (mechanism × sector) matrix, not
just grouping. ~3,564 rows are flagged as viable micro-business
launch candidates; ~24 clusters are flagged as "rebuild candidates"
(old-skewed spaces ripe for an AI-first rewrite).

## Base URLs

- **Production (Railway):** https://fabulous-fascination-production-4638.up.railway.app
- **Local dev (FastAPI):** http://127.0.0.1:8080 or :8002 (see `.claude/launch.json`)
- **Local dev (Datasette, DB browser):** http://127.0.0.1:8001 (start with `./scripts/run_datasette.sh`)

All GET endpoints are public — no auth required for reads.

## Quick start — two URLs to remember

Hit these first and you have everything:

```
GET /ideas/api/index.json      # catalog of every endpoint
GET /ideas/api/schema.json     # data dictionary + enum values + tag vocabs
```

The schema endpoint returns:

- `tables` — row counts + descriptions for 7 tables
- `enums` — value domains for era_class, customer_size, business_model,
  moat_type, niche_specificity (with notes on what each value means)
- `vocab.mechanism_tags` — top-100 mechanism tags with usage counts
- `vocab.sector_tags` — top-100 sector tags with usage counts
- `columns` — field-level documentation for every important column
- `programs` — per-source row + extraction counts
- `opportunity_score_buckets` — distribution of score tiers
- `canonical_queries` — URL patterns for common tasks

## Canonical tasks

| Task | URL |
|---|---|
| Ideas about a topic | `GET /ideas/api/search.json?q=invoice+reconciliation` |
| Top launch candidates | `GET /ideas/api/search.json` then filter by `opportunity_score >= 90`, or `GET /ideas/launch-candidates?sort=score` (HTML) |
| One company's full profile | `GET /ideas/api/idea/{id}.json` |
| Cluster + its members | `GET /ideas/api/cluster/{cluster_id}.json` |
| Ranked gaps (mechanism × sector) | `GET /ideas/api/gaps.json` |
| Companies in a specific cell | `GET /ideas/api/gap/{mechanism}/{sector}.json` |
| Mechanism × sector heatmap | `GET /ideas/api/heatmap.json` |
| Full 2D UMAP scatter (~20K points) | `GET /ideas/api/scatter.json` |
| Pipeline stats | `GET /ideas/api/stats.json` |

## Data model (summary)

```
company_ideas (26,514)
├── id          PK
├── program     'Y Combinator' | 'Show HN' | 'Reddit r/microsaas' | ...
├── company     may be empty for Reddit/HN rows
├── one_liner, long_description, tags_json  (from source)
├── normalized_website   — join key to website_enrichment
├── year_founded, batch, country
├── cluster_id           → idea_clusters.cluster_id  (-1 is noise)
├── umap_x, umap_y       — 2D coords for scatter plots
├── opportunity_score    — 0-100 composite, NULL if no extraction
└── opportunity_breakdown — per-signal JSON

idea_extraction (~10,295 rows, joined on company_idea_id → company_ideas.id)
├── problem_statement, customer_verticals
├── mechanism_tags (JSON array), sector_tags (JSON array)
├── customer_size, business_model, moat_type, niche_specificity  (enums)
└── solo_buildable (0/1), ai_first_advantage (0/1) + reasonings

idea_clusters (1,593 parents + subs)
├── cluster_id   PK (-1 = noise bucket)
├── parent_cluster_id  NULL for top-level, otherwise points at a parent
├── label        (auto TF-IDF)        — prefer llm_label when set
├── llm_label    — human-readable (may be NULL until relabel job runs)
├── era_class    — hot / steady / rebuild_candidate / legacy / unknown
└── size, median_year, count_pre_2015, count_2015_2022, count_2023_plus

website_enrichment (2,718, joined on normalized_website)
└── title, meta_description, hero_h1, hero_text

idea_gap_ranking (~1,000)
└── pre-scored (mechanism, sector) recombination gaps + reasoning

tag_alias (~2,794)
└── variant → canonical tag mapping (mechanism + sector axes)
```

Full field docs are at `/ideas/api/schema.json` under `.columns`.

## Value-domain cheat sheet

- `era_class`: `hot` (≥50% founded 2023+), `rebuild_candidate` (old space, no recent entries — AI-first rewrite plausible), `steady`, `legacy`, `unknown`
- `customer_size`: `consumer | prosumer | smb | mid_market | enterprise | developer`
- `business_model`: `saas | service | marketplace | api | hardware | agency | course | community | consumer_app | hybrid`
- `moat_type`: `none | brand | integration | domain_expertise | data | network | regulatory | capital`
  - **`regulatory` and `capital` disqualify the micro-business shape.** Filter them out when looking for launch candidates.
  - **`none` is often GOOD for this use case** — means space is contestable.
- `niche_specificity`: `narrow | medium | broad` (narrower = more defensibly scoped)
- `solo_buildable`, `ai_first_advantage`: `0 | 1`

## Arbitrary SQL (local only)

If you're running locally, Datasette at `:8001/handelsregister` accepts
arbitrary read-only SQL:

```
GET /handelsregister.json?sql=SELECT+program,COUNT(*)+FROM+company_ideas+GROUP+BY+1&_shape=array
```

The production Railway deployment does **not** expose Datasette —
production is FastAPI-only.

## Writing data (auth required)

The only writeable endpoint is `POST /admin/ideas/seed`, which replaces
the idea tables from a gzipped SQL dump. It requires header
`X-Seed-Token: <token>` matching the `IDEAS_SEED_TOKEN` env var on the
server. Don't call this unless you're the pipeline owner.

There's also `POST /ideas/api/gap-feedback` for recording a thumbs-up /
thumbs-down on a gap (no auth; tracks per-user sentiment server-side).

## Limitations (honest)

- **Only 39% of rows have LLM extraction** (mostly Show HN untagged —
  extraction was credit-limited). Filter `opportunity_score IS NOT NULL`
  to work only with extracted rows.
- **Cluster labels are auto-generated TF-IDF** (e.g. `"artificial
  intelligence — imessage"`). A Claude-generated relabel job exists but
  hasn't run yet — `idea_clusters.llm_label` may be NULL.
- **Website enrichment covers 19% of unique domains.** Most Show HN and
  Reddit rows link to news.ycombinator.com or reddit.com, not the
  product site, so no enrichment signal there.
- **Mechanism tags have ~2,360 distinct values** after canonicalization.
  A long-tail exists — most are rare; the top 100 (in `vocab.mechanism_tags`)
  cover the majority of usage.
- **No pagination contract.** Many endpoints return full result sets.
  Watch for large responses on `/ideas/api/scatter.json` (~2 MB).
- **This is a living project — schemas may shift.** Re-fetch
  `/ideas/api/schema.json` at the start of a session; don't cache
  assumptions.

## Related files in this repo

- `persistence/database.py` — SQLite schema owner
- `scheduler/jobs/idea_*.py` — pipeline jobs (scraper loader, clustering,
  LLM extraction, canonicalization, opportunity scoring, UMAP 2D)
- `web/routers/ideas.py` — all `/ideas/*` routes
- `scripts/validate_pipeline.py` — 57-check end-to-end validator
- `scripts/dump_idea_tables.py` — export idea tables for seeding
- `datasette/metadata.json` — canned queries + facets for the local
  Datasette instance

## Getting stuck?

Start with `curl <base>/ideas/api/schema.json | jq`. Most agent
confusion dissolves after reading that response.
