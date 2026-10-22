"""`mtgsync` command line: one subcommand per stage, plus `run` for stages 1-3."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .config import Settings, load_settings
from .enrich import enrich_rows, write_unmatched_csv
from .moxfield import read_moxfield_csv
from .price_stage import price_records, write_needs_manual_price, write_price_comparison
from .records import ENRICHED_FILENAME, PRICED_FILENAME, read_jsonl, write_jsonl
from .scryfall import load_index
from .square_build import (apply_square_export, build_rows, load_template_columns, record_batch,
                           restrict_to_items, write_bulk_detail_csv, write_import_csv, write_import_xlsx,
                           write_orphans_csv, write_summary_md)


def _say(msg: str) -> None:
    print(msg, file=sys.stderr)


def _settings(args: argparse.Namespace) -> Settings:
    s = load_settings(args.config, root=args.root)
    if getattr(args, "threshold", None) is not None:
        s.set("tier.threshold_usd", args.threshold)
    if getattr(args, "price_source", None):
        s.set("pricing.source", args.price_source)
    if getattr(args, "location", None):
        s.set("square.location_name", args.location)
    if getattr(args, "template", None):
        s.set("square.template", args.template)
    if getattr(args, "category", None):
        s.set("square.category", args.category)
    for key in ("paths.cache", "paths.work", "paths.output", "paths.state"):
        s.path(key).mkdir(parents=True, exist_ok=True)
    return s


# --- stages -----------------------------------------------------------------------------


def cmd_enrich(args: argparse.Namespace, settings: Settings | None = None) -> int:
    s = settings or _settings(args)
    out = s.path("paths.output")
    rows, missing_optional, row_errors = read_moxfield_csv(args.moxfield_csv)
    _say(f"Moxfield: {len(rows)} rows read")
    if missing_optional:
        _say(f"  optional columns absent: {', '.join(missing_optional)}")
    for err in row_errors[:20]:
        _say("  row error: " + err)
    if len(row_errors) > 20:
        _say(f"  ... {len(row_errors) - 20} more row errors")

    index = load_index(s.path("paths.cache"), ttl_hours=float(s.get("scryfall.cache_ttl_hours", 24)),
                       offline=bool(args.offline), user_agent=str(s.get("scryfall.user_agent")),
                       bulk_type=str(s.get("scryfall.bulk_type", "default_cards")), log=_say)
    _say(f"Scryfall: {len(index)} cards indexed")

    records, unmatched, stats = enrich_rows(rows, index, s)
    write_unmatched_csv(out / "unmatched.csv", unmatched)
    n = write_jsonl(s.path("paths.work") / ENRICHED_FILENAME, records)
    _say(f"Enriched: {n} records written; {stats.unmatched} unmatched (output/unmatched.csv); "
         f"{stats.lang_fallbacks} non-English rows matched to the English object; "
         f"{stats.proxies_dropped} proxies dropped; {stats.non_english_dropped} non-English dropped; "
         f"{stats.zero_count_dropped} zero-count dropped")
    for w in stats.finish_mismatches[:10]:
        _say("  finish mismatch: " + w)
    if len(stats.finish_mismatches) > 10:
        _say(f"  ... {len(stats.finish_mismatches) - 10} more finish mismatches")
    if stats.rows_in and stats.unmatched / stats.rows_in > 0.01:
        _say("  WARNING: more than 1% of rows unmatched; check output/unmatched.csv and "
             "config/set_code_overrides.yaml")
    return 0


def cmd_price(args: argparse.Namespace, settings: Settings | None = None) -> int:
    s = settings or _settings(args)
    work, out = s.path("paths.work"), s.path("paths.output")
    src = work / ENRICHED_FILENAME
    if not src.exists():
        _say(f"{src} not found; run `mtgsync enrich <moxfield.csv>` first")
        return 2
    records = list(read_jsonl(src))

    if args.compare_prices:
        n = write_price_comparison(out / "price_comparison.csv", records, s)
        _say(f"Comparison mode: {n} rows written to output/price_comparison.csv; nothing priced or imported")
        return 0

    stats = price_records(records, s, source_name=args.price_source)
    write_jsonl(work / PRICED_FILENAME, records)
    n_manual = write_needs_manual_price(out / "needs_manual_price.csv", records)
    _say(f"Priced: {stats.priced} with a base price, {stats.null_price} without "
         f"({n_manual} listed in output/needs_manual_price.csv); "
         f"{stats.tracked} tracked / {stats.bulk} bulk "
         f"(threshold ${float(s.get('tier.threshold_usd', 0)):.2f})")
    if stats.fallback_to_scryfall:
        _say(f"  {stats.fallback_to_scryfall} records fell back to Scryfall prices")
    for w in stats.warnings[:20]:
        _say("  " + w)
    return 0


def cmd_build(args: argparse.Namespace, settings: Settings | None = None) -> int:
    s = settings or _settings(args)
    work, out = s.path("paths.work"), s.path("paths.output")
    src = work / PRICED_FILENAME
    if not src.exists():
        _say(f"{src} not found; run `mtgsync price` first")
        return 2
    records = list(read_jsonl(src))

    cols = load_template_columns(s)
    if cols.source == "builtin":
        _say(f"WARNING: Square template {s.path('square.template')} not found; using built-in headers")
    else:
        _say(f"Square template: {cols.template_path} (header row {cols.header_row}, "
             f"quantity column '{cols.new_quantity}')")
    result = build_rows(records, s)
    if args.limit_items or args.only_items:
        only = [n for n in (args.only_items or "").split(",") if n.strip()] or None
        result = restrict_to_items(result, int(args.limit_items) if args.limit_items else None, only)
        names = sorted({r.item_name for r in result.rows})
        _say(f"Test run: {len(names)} items, {len(result.rows)} variations, "
             f"category '{s.get('square.category')}'")
        for n in names:
            _say("  " + n)

    mode = (args.quantity_mode or str(s.get("square.quantity_mode", "set"))).lower()
    export_match = None
    if args.square_export:
        export_match = apply_square_export(result.rows, Path(args.square_export),
                                           str(s.get("square.location_name", "") or ""),
                                           bool(args.zero_missing), quantity_mode=mode)
        write_orphans_csv(out / "orphaned_skus.csv", export_match.orphans)
        _say(f"Update run ({mode}): {export_match.tokens_applied} tokens matched, "
             f"{export_match.added_to_existing} quantities added onto Square stock, "
             f"{len(export_match.orphans)} SKUs in Square not in this file, {export_match.zeroed} zeroed")
    elif mode == "add" and not args.no_square_export:
        _say("add mode needs a fresh Square library export so existing SKUs update in place "
             "instead of being created again: pass --square-export <file> "
             "(Items > Item library > Actions > Export library), or --no-square-export "
             "for a library that holds none of these SKUs yet")
        return 2

    tag = f"_{args.tag}" if args.tag else ""
    fmt = args.format or str(s.get("square.format", "xlsx"))
    xlsx_path = out / f"square_import_{date.today().isoformat()}{tag}.{fmt}"
    earlier = record_batch(s.path("paths.state"), result.rows, xlsx_path.name)
    if earlier:
        result.warnings.append(f"this exact batch (same SKUs and quantities) was already built on "
                               f"{earlier}. If it was imported, importing again double-counts stock "
                               f"in add mode. Wipe Moxfield after each successful import.")
        _say("  WARNING: " + result.warnings[-1])
    taxable = s.get("square.taxable", None)
    alert = s.get("square.stock_alert_count", None)
    via_api = str(s.get("square.inventory_via", "api")).lower() == "api"
    kw = dict(taxable=None if taxable is None else bool(taxable),
              stock_alert_count=None if alert is None else int(alert),
              write_quantities=not via_api)
    if via_api:
        _say("inventory_via: api -> quantity and stock-alert columns left blank; run "
             "`mtgsync inventory` after the Dashboard import")
    if fmt == "csv":
        write_import_csv(xlsx_path, cols, result.rows, **kw)
    else:
        write_import_xlsx(xlsx_path, cols, result.rows, **kw,
                          keep_template_rows=bool(s.get("square.keep_template_rows", False)))
    write_bulk_detail_csv(out / "bulk_detail.csv", result.bulk_detail)
    write_summary_md(out / "import_summary.md", result=result, cols=cols, xlsx_path=xlsx_path,
                     export_match=export_match)
    _say(f"Built {xlsx_path} with {len(result.rows)} rows "
         f"(tracked qty {result.by_tier['tracked']}, bulk qty {result.by_tier['bulk']}); "
         f"{len(result.skipped)} rows skipped. See output/import_summary.md")
    for w in result.warnings:
        _say("  " + w)
    return 0


def cmd_images(args: argparse.Namespace, settings: Settings | None = None) -> int:
    from .square_images import SquareClient, load_dotenv, run_images, run_variation_images

    s = settings or _settings(args)
    src = s.path("paths.work") / PRICED_FILENAME
    if not src.exists():
        _say(f"{src} not found; run `mtgsync price` first")
        return 2
    records = list(read_jsonl(src))
    load_dotenv(s.root / ".env")
    token = os.environ.get("SQUARE_ACCESS_TOKEN", "")
    env = os.environ.get("SQUARE_ENV", "sandbox")
    client = None
    if token:
        client = SquareClient(token, env)
        _say(f"Square environment: {env}")
    elif not args.dry_run:
        _say("SQUARE_ACCESS_TOKEN is not set (.env); use --dry-run or add the token")
        return 2
    else:
        _say("No SQUARE_ACCESS_TOKEN; dry run will list planned images without checking Square")

    report = run_images(records, cache_dir=s.path("paths.cache"), state_dir=s.path("paths.state"),
                        user_agent=str(s.get("scryfall.user_agent")),
                        include_language=bool(s.get("sku.include_language", False)),
                        dry_run=bool(args.dry_run), client=client, log=_say)
    _say(f"Images: {report.planned} planned, {report.uploaded} uploaded, "
         f"{report.already_done} already done, {len(report.not_in_square)} items not found in Square, "
         f"{len(report.errors)} errors")
    for name in report.not_in_square[:20]:
        _say("  not in Square: " + name)
    errors = list(report.errors)

    if bool(s.get("images.variations", True)) and not args.no_variations:
        vreport = run_variation_images(records, cache_dir=s.path("paths.cache"), state_dir=s.path("paths.state"),
                                       user_agent=str(s.get("scryfall.user_agent")),
                                       include_language=bool(s.get("sku.include_language", False)),
                                       dry_run=bool(args.dry_run), client=client, log=_say)
        _say(f"Variation images: {vreport.planned} printings planned, {vreport.uploaded} uploaded, "
             f"{vreport.reused} reused, {vreport.attached} variations attached, "
             f"{vreport.already_done} printings already done, {len(vreport.not_in_square)} SKUs not in Square, "
             f"{len(vreport.errors)} errors")
        for sku in vreport.not_in_square[:20]:
            _say("  not in Square: " + sku)
        errors.extend(vreport.errors)
    return 1 if errors else 0


def cmd_inventory(args: argparse.Namespace, settings: Settings | None = None) -> int:
    from .square_images import SquareClient, load_dotenv
    from .square_inventory import run_inventory

    s = settings or _settings(args)
    src = s.path("paths.work") / PRICED_FILENAME
    if not src.exists():
        _say(f"{src} not found; run `mtgsync price` first")
        return 2
    records = list(read_jsonl(src))
    result = build_rows(records, s)
    rows = [r for r in result.rows if r.tier == "tracked"]
    if args.limit_items or args.only_items:
        only = [n for n in (args.only_items or "").split(",") if n.strip()] or None
        rows = restrict_to_items(result, int(args.limit_items) if args.limit_items else None, only).rows

    load_dotenv(s.root / ".env")
    token = os.environ.get("SQUARE_ACCESS_TOKEN", "")
    if not token:
        _say("SQUARE_ACCESS_TOKEN is not set (.env)")
        return 2
    client = SquareClient(token, os.environ.get("SQUARE_ENV", "sandbox"))
    _say(f"Square environment: {client.env}")
    mode = (args.quantity_mode or str(s.get("square.quantity_mode", "set"))).lower()
    alert = s.get("square.stock_alert_count", None)
    report = run_inventory(rows, client=client, location_name=str(s.get("square.location_name", "") or ""),
                           mode=mode, alert_count=None if alert is None else int(alert),
                           state_dir=s.path("paths.state"), dry_run=bool(args.dry_run),
                           force=bool(args.force), log=_say)
    _say(f"Inventory ({report.mode}): {report.planned} variations planned, {report.applied} changes applied, "
         f"{report.tracking_enabled} variations had tracking/alert updated, "
         f"{len(report.not_in_square)} SKUs not in Square, {len(report.errors)} errors")
    for sku in report.not_in_square[:20]:
        _say("  not in Square: " + sku)
    return 1 if report.errors else 0


def cmd_dedupe(args: argparse.Namespace, settings: Settings | None = None) -> int:
    from .square_images import SquareClient, load_dotenv
    from .square_inventory import run_dedupe

    s = settings or _settings(args)
    load_dotenv(s.root / ".env")
    token = os.environ.get("SQUARE_ACCESS_TOKEN", "")
    if not token:
        _say("SQUARE_ACCESS_TOKEN is not set (.env)")
        return 2
    client = SquareClient(token, os.environ.get("SQUARE_ENV", "sandbox"))
    _say(f"Square environment: {client.env}")
    dups, deleted = run_dedupe(client=client, location_name=str(s.get("square.location_name", "") or ""),
                               dry_run=bool(args.dry_run), log=_say)
    _say(f"Dedupe: {len(dups)} duplicate variations found, {len(deleted)} deleted")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    from .pipeline import upload

    s = _settings(args)
    report = upload(Path(args.moxfield_csv), s, dry_run=bool(args.dry_run), force=bool(args.force),
                    offline=bool(args.offline), log=_say)
    for w in report.warnings[:30]:
        _say("  warning: " + w)
    _say(f"Upload{' (dry run)' if report.dry_run else ''}: {report.items_total} items, "
         f"{report.variations_total} variations, {report.quantity_total} cards; created {report.items_created} "
         f"items / {report.variations_created} variations; {report.inventory_applied} inventory changes; "
         f"{report.images_uploaded} images; {len(report.errors)} errors")
    return 0 if report.ok else 1


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import main as gui_main
    return gui_main()


def cmd_run(args: argparse.Namespace) -> int:
    s = _settings(args)
    args.compare_prices = False
    for step in (cmd_enrich, cmd_price, cmd_build):
        rc = step(args, s)
        if rc:
            return rc
    return 0


# --- parser -----------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mtgsync",
                                description="Moxfield collection -> Scryfall -> Square import")
    p.add_argument("--version", action="version", version=f"mtgsync {__version__}")
    p.add_argument("--config", default=None, help="settings file (default config/settings.yaml)")
    p.add_argument("--root", default=None, help="project root for relative paths (default cwd)")
    sub = p.add_subparsers(dest="command", required=True)

    def add_price_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--price-source", choices=["none", "scryfall", "scg"], default=None)
        sp.add_argument("--threshold", type=float, default=None,
                        help="override tier.threshold_usd (0 = Model A)")

    def add_build_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--square-export", default=None,
                        help="Square library export xlsx for an update run (token matching)")
        sp.add_argument("--zero-missing", action="store_true",
                        help="set mode only: quantity 0 on SKUs in Square but absent from Moxfield")
        sp.add_argument("--quantity-mode", choices=["add", "set"], default=None,
                        help="add = Square current + Moxfield count (intake log); set = Moxfield count")
        sp.add_argument("--no-square-export", action="store_true",
                        help="allow add mode without an export (first import into an empty library)")
        sp.add_argument("--location", default=None, help="override square.location_name")
        sp.add_argument("--template", default=None, help="override square.template path")
        sp.add_argument("--category", default=None, help="override square.category")
        sp.add_argument("--limit-items", type=int, default=None, metavar="N",
                        help="test run: keep only the first N items (all their variations)")
        sp.add_argument("--only-items", default=None, metavar="NAME[,NAME...]",
                        help="keep only these item names (comma-separated, case-insensitive)")
        sp.add_argument("--tag", default=None, help="suffix for the output file name, e.g. test")
        sp.add_argument("--format", choices=["xlsx", "csv"], default=None,
                        help="import file format (default square.format in settings, xlsx)")

    sp = sub.add_parser("enrich", help="Stage 1: join Moxfield rows to Scryfall")
    sp.add_argument("moxfield_csv")
    sp.add_argument("--offline", action="store_true", help="never download Scryfall bulk data")
    sp.set_defaults(func=cmd_enrich)

    sp = sub.add_parser("price", help="Stage 2: base and sell prices, tiering")
    add_price_flags(sp)
    sp.add_argument("--compare-prices", action="store_true",
                    help="write output/price_comparison.csv (Scryfall vs SCG); import nothing")
    sp.set_defaults(func=cmd_price)

    sp = sub.add_parser("build", help="Stage 3: write the Square import xlsx")
    add_build_flags(sp)
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("images", help="Stage 4: upload images through the Catalog API")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--no-variations", action="store_true",
                    help="item images only; skip the per-variation (per printing) images")
    sp.set_defaults(func=cmd_images)

    sp = sub.add_parser("inventory", help="Set or add stock counts through the Inventory API")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--quantity-mode", choices=["add", "set"], default=None,
                    help="set = PHYSICAL_COUNT to the Moxfield count; add = ADJUSTMENT by it")
    sp.add_argument("--force", action="store_true", help="add mode: re-apply an already applied batch")
    sp.add_argument("--limit-items", type=int, default=None, metavar="N")
    sp.add_argument("--only-items", default=None, metavar="NAME[,NAME...]")
    sp.set_defaults(func=cmd_inventory)

    sp = sub.add_parser("upload", help="One click: Moxfield CSV -> Square catalog + stock + images (API only)")
    sp.add_argument("moxfield_csv")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--force", action="store_true", help="re-add a batch that was already uploaded")
    sp.add_argument("--offline", action="store_true")
    sp.set_defaults(func=cmd_upload)

    sp = sub.add_parser("gui", help="Open the Windows window")
    sp.set_defaults(func=cmd_gui)

    sp = sub.add_parser("dedupe", help="Delete newer duplicate variations (same SKU within an item)")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_dedupe)

    sp = sub.add_parser("run", help="Stages 1 to 3 in sequence")
    sp.add_argument("moxfield_csv")
    sp.add_argument("--offline", action="store_true")
    add_price_flags(sp)
    add_build_flags(sp)
    sp.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001 - CLI boundary: short message, non-zero exit
        _say(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
