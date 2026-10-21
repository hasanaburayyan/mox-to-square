# mtgsync

Moxfield collection export → Scryfall enrichment → Square item-library import.
Implements the pipeline described in [updated-project-writeup.md](updated-project-writeup.md).

## The Windows app (one click)

`dist\MtgSync\MtgSync.exe` is the whole pipeline behind one button: choose the Moxfield export,
click **Upload to Square**. It enriches from Scryfall, creates any new items and variations
through the Catalog API (no Dashboard import, no template), adds every row's count to stock,
attaches card images to new items, then reminds you to clear the Moxfield collection. A
**Dry run** checkbox shows what would happen without changing anything. The same Moxfield
file is refused a second time unless **Force** is ticked.

**Square settings…** in the window takes the access token (masked), the environment, has a
Test connection button that lists the account's locations, and saves to the `.env` file next to
the exe. The main window shows whether a token is saved. The `dist\MtgSync` folder is
self-contained: keep `config\settings.yaml`, `.env` (the Square token) and the `state` folder
next to the exe. `cache` holds the Scryfall data (re-downloaded
after 24 h). `output\upload_report.md` and `output\unmatched.csv` are written on every run.
Rebuild after code changes with `.\build_exe.ps1`. `MtgSync.exe upload <csv> --dry-run` runs
the same thing from a terminal.

## Setup

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/pytest
```

Then:

1. Export your collection from Moxfield (Collection page → Export CSV). The current one is
   `moxfield_export.csv` in the project root.
2. The blank import template downloaded from Square Dashboard (Items → Item library → Actions →
   Import) is `square-template.xlsx`; `square.template` in `config/settings.yaml` points at it.
   Without it a built-in header list is used and the summary warns you. The template's real
   header is on row 6 under Square's instruction rows; the builder finds it by name and uses
   only that header row. The import file itself is written as **CSV** (header on line 1, data
   from line 2). That is what Square's importer accepted; the xlsx files openpyxl writes were
   rejected with "add a header to column N". Upload the CSV directly and never open and
   re-save it in a spreadsheet tool, which can mangle SKUs. `--format xlsx` still exists.
3. `square.location_name` must match the location in the template's quantity column header
   (`New Quantity The Warp Gate`). Already set.

## Commands

```bash
.venv/Scripts/mtgsync run moxfield_export.csv           # stages 1-3
.venv/Scripts/mtgsync enrich moxfield_export.csv        # stage 1 only
.venv/Scripts/mtgsync price [--price-source scg] [--compare-prices] [--threshold 1.00]
.venv/Scripts/mtgsync build [--square-export input/square_export.xlsx] [--zero-missing]
.venv/Scripts/mtgsync build --limit-items 20 --category "MTG Singles TEST" --tag test   # small test import
.venv/Scripts/mtgsync inventory [--dry-run] [--quantity-mode add|set]   # stock counts via API, needs .env
.venv/Scripts/mtgsync images [--dry-run]               # stage 4, needs .env
.venv/Scripts/mtgsync dedupe [--dry-run]               # remove duplicate variations after a tokenless re-import
.venv/Scripts/mtgsync upload <moxfield.csv> [--dry-run] [--force]   # the one-click path (what the exe does)
.venv/Scripts/mtgsync gui                              # the window, from the venv
```

The first `enrich` downloads Scryfall's `default_cards` bulk file (several hundred MB) into
`cache/` and writes a trimmed index next to it; later runs reuse both. `--offline` skips the
download check.

## What each stage writes

| Stage | Writes |
| --- | --- |
| enrich | `work/enriched.jsonl`, `output/unmatched.csv` |
| price | `work/priced.jsonl`, `output/needs_manual_price.csv`, `state/tracked.json`; with `--compare-prices` only `output/price_comparison.csv` |
| build | `output/square_import_<date>[_tag].csv`, `output/import_summary.md`, `output/bulk_detail.csv`, and `output/orphaned_skus.csv` on update runs |
| images | `cache/images/*.jpg`, `state/images_uploaded.json` |

## Model A vs Model C

`tier.threshold_usd: 0` (default) tracks every card as its own variation (Model A). Set it to a
positive number to switch to Model C: printings whose base price is below it for every finish
roll up into the bulk buckets defined in `tier.bulk_buckets`, and the per-card detail goes to
`output/bulk_detail.csv`. Once a printing is tracked it stays tracked across runs
(`state/tracked.json`).

## Intake workflow (quantity mode `add`, inventory via API)

Moxfield is the intake log and Square is the system of record. Items are created through the
Dashboard import (which shows a preview); stock counts go through the Inventory API, because
the Dashboard import silently ignores quantity columns whose location name does not match.
Each cycle:

1. Add new stock to the Moxfield collection and export it.
2. In Square: Items → Item library → Actions → **Export library**, save as `input/square_export.xlsx`.
3. `mtgsync run <export.csv> --square-export input/square_export.xlsx`. Rows whose SKU already
   exists get Square's Token (so they update in place); new SKUs are created. Prices are left
   blank (`pricing.source: none`) and set in Square. Quantity columns are blank too.
4. Import the CSV in the Dashboard, check the preview, confirm.
5. `mtgsync inventory` adds the Moxfield counts onto Square's stock (ADJUSTMENT per
   variation) and enables tracking plus the low-stock alert on new variations. Use `--dry-run`
   first. A batch that was already applied is refused (`state/inventory_batches.json`).
6. `mtgsync images` to attach scans to any newly created items.
7. **Delete the imported rows from the Moxfield collection**, so the next export only holds new
   stock. The build also records every batch in `state/import_batches.json` and warns if the
   exact same SKUs and quantities are built again, the sign that step 7 was skipped.

**How the Dashboard import matches rows** (observed 2026-10-18): an *item* is matched by Item
Name, but a *variation* is matched only by Token. SKU and Variation Name are not keys. A
re-import without Tokens therefore adds no new items but duplicates every variation (and its
stock). That is why step 2 is required, and why `mtgsync dedupe` exists: it deletes the newer
copy of any variation that shares its SKU with another under the same item (`--dry-run` first).
Quantity and stock-alert columns are applied only when the location name in their header
matches a location in the account exactly; otherwise they are ignored without any error.

For a library that holds none of the SKUs yet, `--no-square-export` skips step 2 and
`mtgsync inventory --quantity-mode set` writes absolute counts. To treat Moxfield as the full
inventory instead, set `square.quantity_mode: set`; `--zero-missing` only applies in that mode.

## Non-English cards

Scryfall's `default_cards` file has one object per card (English, or the printed language when
that is the only one). A non-English Moxfield row whose language has no own object is matched
to the English object for the same set and collector number; the record keeps the row's
language and is flagged `lang_fallback`. With `sku.include_language: false` (default) such a
row shares the English SKU and its quantity is merged in; set it to true to get a `-ja` style
suffix and its own variation instead.

## Status / not yet done

- The Moxfield headers and condition wording in `mtgsync/moxfield.py` were verified against
  `moxfield_export.csv` (Oct 2026). The reader fails loudly if a required column goes missing.
- `square-template.xlsx` is the real template; the built-in fallback headers are only a stand-in
  for when it is absent and should not be used for a real import.
- The Star City Games price source has the cache, rate limit, cap and fallback plumbing, but
  no live fetcher. Its storefront search endpoint and terms of service need to be confirmed
  first; until then `--price-source scg` falls back to Scryfall for everything.
- Stage 4 (`images`) is written against Square's REST API but has not been run against a real
  account. Use `SQUARE_ENV=sandbox` and `--dry-run` first.
- Condition multipliers (`pricing.condition_multipliers`) default to 1.0 for every condition
  until the condition policy is decided.
