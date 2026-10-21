# Moxfield to Square Migration: Project Brief

Oct 18, 2026 · @hasan

Build a repeatable pipeline that takes a Moxfield collection export, enriches it with Scryfall data and prices, and produces a Square item-library import plus an image upload job. The pipeline must be re-runnable for weekly price refreshes, and must carry a flag to test Star City Games prices as an alternative source. This brief is written to hand to a coding agent to start the project.

## Pipeline overview

&#91;embedded content: pipeline · 4 stages, 2 price sources, 1 manual step\]

Stages 1 to 3 run as one command and produce a file; the Dashboard import is the only manual step, and stage 4 runs after it because Square's import file cannot carry images. Scryfall feeds both the enrichment and the default prices; Star City Games is an optional second price source that is off unless the flag is set.

## Inputs

The only input is the Moxfield collection CSV, exported from the Collection page. One row per printing, condition, finish and language. Treat it as read-only; every run starts from a fresh export.

| Moxfield column | Used for |  |
| --- | --- | --- |
| Count | Square quantity |  |
| Tradelist Count | Ignore, or subtract from Count if trade stock is held back |  |
| Name | Item name; double-faced cards use `//` |  |
| Edition | Set code (lowercase); Scryfall join key |  |
| Collector Number | Scryfall join key; may contain letters (`123a`, `★`) |  |
| Condition | NM / LP / MP / HP; part of the SKU and variation name |  |
| Language | Filter or suffix; non-English printings share a collector number |  |
| Foil | `foil`, `etched` or blank; selects the price field and SKU suffix |  |
| Tags, Alter, Proxy | Exclude rows flagged Proxy; Alter and Tags are informational |  |
| Purchase Price | Optional cost basis; not imported to Square |  |

Verify the exact header names against a real export before coding, since Moxfield has changed them before.

## Data model

**Models considered**

Three ways to structure singles were weighed (full comparison in *MTG Singles in Square: Three Models*):

| Model | Structure | Best for | Main drawback |
| --- | --- | --- | --- |
| A: Name + variations | One item per card name; one variation per printing, finish and condition | Clean register search; one image per name; every card tracked | Variation cap on basic lands and heavy reprints; large library |
| B: Flat per printing | One item per printing; finish and condition as variations | Simplest 1:1 script; image per printing | Library balloons; cluttered search and online store |
| C: Tiered | Model A structure for cards above a price threshold; bulk buckets below | Small, fast library; small price and image jobs | Bulk-tier stock lives outside Square; two systems of record |

**Decision: start with Model A for the test run.** The pipeline is built so the structure is a config choice: `tier.threshold_usd` set to `0` produces pure Model A, and raising it switches to Model C with no code change. The test import and first live import run as Model A so every card is in Square and the register search can be evaluated; the threshold is revisited after the first weeks of sales, when the long tail's actual volume is known.

**Structure (applies to Model A and to the tracked tier of Model C)**

**Tracked tier**

- Item name: the card name as Scryfall prints it (`Lightning Bolt`, `Fable of the Mirror-Breaker // Reflection of Kiki-Jiki`).
- Variation name: `<SET NAME> #<num> · <condition> · <finish>`, for example `Core Set 2021 #159 · NM · Foil`. The full set name goes here so it shows on the register and receipt.
- SKU: `<set_code>-<collector_number>-<finish>-<condition>`, lowercased, finish in {`nf`, `f`, `e`}, for example `m21-159-f-nm`. Set code stays in the SKU because it is the stable join key for Scryfall; the full set name lives in the variation name. Config option to append a language suffix (`-ja`) for non-English rows.
- Category: `MTG Singles` (config). Optional sub-category by set, off by default.
- Description: Scryfall type line and set name, so the item is searchable by either.

**Bulk tier**

- One item `Bulk Common/Uncommon` with variations `Nonfoil` and `Foil`; one item `Bulk Rare/Mythic` with the same. Bucket definitions and prices are config.
- Quantity on bulk variations is the sum of Count across every row that fell into the bucket, so Square still carries a total.
- The per-card detail for the bulk tier is written to `output/bulk_detail.csv` for reference; it does not go to Square.

**Promotion rule**

- A card is tracked when `base_price >= threshold` for any of its finishes. Once a card is tracked it stays tracked on later runs (the tracked set is persisted in `state/tracked.json`) so a price dip does not delete it from Square.

## Stage 1: Normalize and enrich with Scryfall

Join every Moxfield row to its Scryfall card object using the `default_cards` bulk file, never per-card API calls.

1. Fetch the bulk-data index at `https://api.scryfall.com/bulk-data`, find the `default_cards` entry, download its `download_uri` to `cache/scryfall_default_cards.json` (a few hundred MB). Skip the download if the cached file is less than 24 hours old or `--offline` is set. Send a descriptive `User-Agent` header; Scryfall requires one.
2. Index the file by `(set, collector_number)` into a dict. Keep per card: `id` (Scryfall UUID), `name`, `set`, `set_name`, `collector_number`, `rarity`, `lang`, `type_line`, `finishes`, `prices`, `image_uris` or `card_faces[0].image_uris` for double-faced cards.
3. For each Moxfield row: lowercase Edition, strip whitespace from Collector Number, look up the key. Non-English rows need `lang` matched as well, since Scryfall treats each language as a separate card object with the same collector number.
4. Rows that miss the join go to `output/unmatched.csv` with the reason. Expect a handful: Moxfield uses a few promo set codes that differ from Scryfall's. Maintain a small `config/set_code_overrides.yaml` for those.
5. Drop rows flagged Proxy. Emit the normalized record to `work/enriched.parquet` (or JSONL) so later stages never re-read Moxfield.

Output of this stage: one enriched record per Moxfield row with the Scryfall ID, display names, rarity, finish and the raw price block.

## Stage 2: Pricing

Every record gets a `base_price` from a price source, then a `sell_price` from the pricing rule. The source is selected by config so SCG can be tested side by side with Scryfall without touching the rest of the pipeline.

**Price source interface**

Define one interface, `PriceSource.get(scryfall_id, set, collector_number, finish) -> PriceQuote | None`, where a quote carries `price`, `currency`, `source`, `as_of` and `condition`. Two implementations ship:

- `ScryfallPriceSource` (default): reads `prices.usd`, `prices.usd_foil` or `prices.usd_etched` from the enriched record by finish. No network; the bulk file already has it. Returns `None` when the field is null.
- `StarCityPriceSource` (behind `--price-source scg` or `pricing.source: scg` in config): queries SCG's storefront search backend by card name, filters results to the matching set and collector number, and returns the in-stock NM price (or the lowest in-stock price if NM is absent). SCG has no official API, so this is a scraper. Requirements: a request cache on disk keyed by card name with a configurable TTL so re-runs do not re-hit the site; a rate limit (start at 1 request per 2 seconds); a hard cap on cards per run (`pricing.scg.max_cards`, default 200) so the first tests stay small; graceful fallback to Scryfall when SCG returns nothing or errors. Review SCG's terms of service before running this at scale, and keep it opt-in.

**Comparison mode**

`--compare-prices` runs both sources and writes `output/price_comparison.csv` with Scryfall price, SCG price, the ratio, and in-stock status per card. Nothing is imported in this mode. This is how the SCG flag gets evaluated: run it on the tracked tier, look at the ratio distribution, decide whether SCG is worth keeping.

**Pricing rule** (config, applied to `base_price`)

- `markup`: multiplier, default 1.0 for Scryfall market price.
- `round_to`: round up to the nearest increment, default $0.25.
- `floor`: minimum sell price for a tracked card, default equal to the matching bulk bucket price.
- Null base price: assign the bulk bucket price and add the row to `output/needs_manual_price.csv`.

Output: each record now has `base_price`, `price_source`, `price_as_of`, `sell_price` and `tier` (tracked or bulk).

## Stage 3: Build the Square import file

Produce an `.xlsx` (not CSV, to avoid spreadsheet tools mangling SKUs) matching the blank import template downloaded from Square Dashboard under Items > Item library. The template's header row is the contract; read it from `config/square_template.xlsx` at runtime rather than hard-coding column names, since Square changes them.

1. Group tracked records by card name into items; each record becomes one variation row under it. Square's template uses repeated Item Name rows with distinct Variation Name values.
2. Fill Item Name, Variation Name, SKU, Category, Description, Price, and the quantity column for the configured location (`New Quantity [<Location Name>]`). Set Item Type blank so items import as Standard. Set `Stock Alert Enabled` to N by default.
3. Append the bulk items with their summed quantities.
4. Validate before writing: unique SKUs, no empty prices, no item with more than the variation cap (config, default 250; split basic lands by set if exceeded), total row count.
5. Write `output/square_import_<date>.xlsx`, plus `output/import_summary.md` with counts by tier, by set, and any warnings.

**Update runs.** For a re-run against a live library, first export the current library from Square (`Export library` on the same page) to `input/square_export.xlsx`, and match on SKU to carry over the `Token` column so Square updates rows in place instead of creating duplicates. New SKUs get no token and are created. SKUs in Square but absent from Moxfield are listed in `output/orphaned_skus.csv` and left untouched unless `--zero-missing` is passed, which sets their quantity to 0.

The upload itself stays manual in the Dashboard for v1 (Items > Item library > Actions > Import). A later version can push through the Catalog API's batch upsert instead.

## Stage 4: Images via the Square Catalog API

Square's import file cannot carry images, so this is a separate job run after the import has landed.

1. Export the library from Square (or call the Catalog API `ListCatalog` with `types=ITEM,ITEM_VARIATION`) to get Square's object IDs keyed by SKU.
2. For each tracked item (one image per card name), pick the image: the `normal` size `image_uris.normal` from the record of the highest-value printing in stock, or the first printing alphabetically by set. Double-faced cards use `card_faces[0]`.
3. Download the image to `cache/images/<scryfall_id>.jpg` (skip if present). Scryfall asks that images be cached, not hot-linked.
4. Call `CreateCatalogImage` with the item's ID and the file. Square accepts JPEG and PNG; keep files under 15 MB (Scryfall's `normal` is \~100 KB).
5. Record success in `state/images_uploaded.json` keyed by SKU so re-runs only upload new items. Rate-limit to a few requests per second and retry on 429.

Needs a Square access token with `ITEMS_WRITE` scope, stored in `.env` as `SQUARE_ACCESS_TOKEN`, never in config or the repo. Start in the Square sandbox environment (`SQUARE_ENV=sandbox`) to verify the flow before pointing at production.

## Configuration, flags, repo layout and tooling

Python 3.12 with `pandas`, `openpyxl`, `requests`, `pyyaml`, `squareup` (official SDK) and `pytest`. Python fits this better than Java: it is a batch data job, and every library involved has a first-class Python client. A single CLI entry point, `mtgsync`, with subcommands per stage so each can be run and tested alone.

**Commands**

| Command | What it does |
| --- | --- |
| `mtgsync enrich <moxfield.csv>` | Stage 1; writes `work/enriched.*` and `output/unmatched.csv` |
| `mtgsync price [--price-source scryfall\|scg] [--compare-prices]` | Stage 2 |
| `mtgsync build [--square-export <file>] [--zero-missing]` | Stage 3; writes the import xlsx |
| `mtgsync images [--dry-run]` | Stage 4 |
| `mtgsync run <moxfield.csv>` | Stages 1 to 3 in sequence |

**Config** (`config/settings.yaml`, every value overridable by CLI flag)

- `tier.threshold_usd: 0 (Model A for the test run; raise to 1.00 to switch to Model C)`
- `tier.bulk_buckets`: list of `{name, rarities, finish, price}`
- `sku.include_language: false`
- `square.location_name`, `square.category`, `square.variation_cap: 250`
- `pricing.source: scryfall`, `pricing.markup: 1.0`, `pricing.round_to: 0.25`
- `pricing.scg.enabled: false`, `pricing.scg.rate_limit_seconds: 2`, `pricing.scg.max_cards: 200`, `pricing.scg.cache_ttl_hours: 168`
- `scryfall.cache_ttl_hours: 24`

**Repo layout**

```
mtgsync/
  cli.py
  moxfield.py        # CSV reader, header validation
  scryfall.py        # bulk download, index, lookup
  pricing/
    base.py          # PriceSource interface, PricingRule
    scryfall.py
    scg.py
  square_build.py    # xlsx writer, token matching
  square_images.py   # Catalog API image upload
  tiering.py
config/
  settings.yaml
  set_code_overrides.yaml
  square_template.xlsx
tests/
  fixtures/          # 50-row Moxfield sample, trimmed Scryfall JSON
cache/   work/   output/   state/   (gitignored)
```

Secrets live in `.env` only. `cache/`, `work/`, `output/` and `state/` are gitignored; `state/tracked.json` and `state/images_uploaded.json` are the only files that need backing up between runs.

## Milestones, test plan and open decisions

**Milestones**

1. Stage 1 working against a real Moxfield export with under 1% unmatched rows.
2. Stage 2 with Scryfall source and the pricing rule; `price_comparison.csv` produced in compare mode with SCG on a 50-card sample.
3. Stage 3 producing an xlsx that imports clean into a test category of \~100 cards; register search and a test sale verified; test category deleted.
4. Update run against that test import proving token matching updates in place.
5. Full import to production, then Stage 4 images on the tracked tier.
6. Weekly scheduled run for price refresh.

**Test plan**

- Unit tests on fixtures: SKU generation (including `//` names, `★` collector numbers, etched finish), tier assignment at the threshold boundary, pricing rule rounding and floor, token matching with a fake Square export.
- Contract test that fails loudly if the Moxfield headers or Square template headers change.
- The SCG source is tested only through recorded HTTP fixtures, never live, in CI.

**Open decisions** (block the first live import, not the build)

- [ ] Threshold value and bulk bucket prices, once the Model A test run has been evaluated
- [ ] Conditions to track beyond NM
- [ ] Whether non-English cards are tracked, bulked or excluded
- [ ] Language suffix in SKU
- [ ] Markup and rounding rule
- [ ] SCG terms of service reviewed before any scheduled scraping
- [ ] Square location name as it appears in the Dashboard
- [ ] Whether Square Online will list singles (changes image priority)
