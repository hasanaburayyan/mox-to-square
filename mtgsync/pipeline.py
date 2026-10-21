"""One-click upload: Moxfield CSV -> Scryfall -> Square (catalog, inventory, images).

No Dashboard import, no template, no library export. Moxfield is the intake log: every
row's count is ADDED to Square's stock, and the same batch is refused a second time.
"""

from __future__ import annotations

import hashlib
import json
import os
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import Settings
from .enrich import enrich_rows, write_unmatched_csv
from .moxfield import read_moxfield_csv
from .price_stage import price_records
from .records import ENRICHED_FILENAME, PRICED_FILENAME, write_jsonl
from .scryfall import load_index
from .square_build import build_rows
from .square_catalog import apply_catalog, plan_catalog
from .square_images import SquareClient, load_dotenv, run_images
from .square_inventory import InventoryError, run_inventory

UPLOADS_STATE_FILENAME = "uploads.json"
Log = Callable[[str], None]


class UploadError(RuntimeError):
    pass


@dataclass
class UploadReport:
    moxfield_rows: int = 0
    unmatched: int = 0
    dropped: int = 0
    items_total: int = 0
    variations_total: int = 0
    quantity_total: int = 0
    items_created: int = 0
    variations_created: int = 0
    items_extended: int = 0
    inventory_applied: int = 0
    images_uploaded: int = 0
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _uploads(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("uploads", [])


def make_client(settings: Settings) -> SquareClient:
    from .env_store import square_credentials
    token, env = square_credentials(settings.root)
    if not token:
        raise UploadError("No Square access token. Click 'Square settings…' (or put SQUARE_ACCESS_TOKEN "
                          "in the .env file next to the app).")
    return SquareClient(token, env)


def upload(moxfield_csv: Path, settings: Settings, *, dry_run: bool = False, force: bool = False,
           offline: bool = False, client: SquareClient | None = None, log: Log = print) -> UploadReport:
    report = UploadReport(dry_run=dry_run)
    try:
        _upload(moxfield_csv, settings, report, dry_run=dry_run, force=force, offline=offline,
                client=client, log=log)
    except (UploadError, InventoryError) as exc:
        report.errors.append(str(exc))
        log("ERROR: " + str(exc))
    except Exception as exc:  # noqa: BLE001 - surface anything else in the window/log
        report.errors.append(f"{type(exc).__name__}: {exc}")
        log("ERROR: " + traceback.format_exc())
    return report


def _upload(moxfield_csv: Path, s: Settings, report: UploadReport, *, dry_run: bool, force: bool,
            offline: bool, client: SquareClient | None, log: Log) -> None:
    moxfield_csv = Path(moxfield_csv)
    if not moxfield_csv.exists():
        raise UploadError(f"file not found: {moxfield_csv}")
    for key in ("paths.cache", "paths.work", "paths.output", "paths.state"):
        s.path(key).mkdir(parents=True, exist_ok=True)
    out, state_dir = s.path("paths.output"), s.path("paths.state")

    # Same file uploaded before?
    fh = _file_hash(moxfield_csv)
    uploads_path = state_dir / UPLOADS_STATE_FILENAME
    earlier = next((u for u in _uploads(uploads_path) if u["hash"] == fh and not u.get("dry_run")), None)
    if earlier and not force:
        raise UploadError(f"this exact Moxfield file was already uploaded on {earlier['at']} "
                          f"({earlier['file']}). Clear the Moxfield collection and export again, "
                          "or tick 'force' if you really want to add it twice.")

    # Stage 1: Moxfield + Scryfall
    log(f"Reading {moxfield_csv.name} ...")
    rows, missing_optional, row_errors = read_moxfield_csv(moxfield_csv)
    report.moxfield_rows = len(rows)
    for err in row_errors[:10]:
        report.warnings.append("row error: " + err)
    log(f"  {len(rows)} rows" + (f", {len(row_errors)} rows skipped (see warnings)" if row_errors else ""))
    log("Loading Scryfall card data (first run downloads ~80 MB and builds an index) ...")
    index = load_index(s.path("paths.cache"), ttl_hours=float(s.get("scryfall.cache_ttl_hours", 24)),
                       offline=offline, user_agent=str(s.get("scryfall.user_agent")),
                       bulk_type=str(s.get("scryfall.bulk_type", "default_cards")), log=log)
    records, unmatched, stats = enrich_rows(rows, index, s)
    write_unmatched_csv(out / "unmatched.csv", unmatched)
    write_jsonl(s.path("paths.work") / ENRICHED_FILENAME, records)
    report.unmatched = stats.unmatched
    report.dropped = stats.proxies_dropped + stats.non_english_dropped + stats.zero_count_dropped
    log(f"  matched {stats.matched}, unmatched {stats.unmatched}, dropped {report.dropped}")
    for u in unmatched[:10]:
        report.warnings.append(f"unmatched line {u.line_no}: {u.name} ({u.edition} #{u.collector_number}): {u.reason}")
    if not records:
        raise UploadError("nothing to upload: no rows matched Scryfall")

    # Stage 2 + 3: pricing (none by default), tiering, rows
    price_records(records, s, state_dir=state_dir)
    write_jsonl(s.path("paths.work") / PRICED_FILENAME, records)
    result = build_rows(records, s)
    rows_out = [r for r in result.rows if r.tier == "tracked"]
    report.warnings.extend(result.warnings)
    report.items_total = len({r.item_name for r in rows_out})
    report.variations_total = len(rows_out)
    report.quantity_total = sum(r.quantity for r in rows_out)
    log(f"  {report.items_total} items, {report.variations_total} variations, {report.quantity_total} cards")

    # Square
    client = client or make_client(s)
    log(f"Connecting to Square ({client.env}) ...")
    items = client.list_items()
    categories = client.list_categories()
    alert = s.get("square.stock_alert_count", None)
    alert = None if alert is None else int(alert)
    plan = plan_catalog(rows_out, items, categories, str(s.get("square.category", "MTG Singles")), alert)
    log(f"  catalog: {len(plan.new_items)} new items, {len(plan.new_variations)} new variations on "
        f"{len(plan.items_extended)} existing items, {plan.existing_variations} variations already exist"
        + ("" if plan.category_id else f", category {plan.category_name!r} will be created"))
    report.items_extended = len(plan.items_extended)

    if dry_run:
        log("Dry run: checking inventory ...")
        run_inventory(rows_out, client=client, location_name=str(s.get("square.location_name", "") or ""),
                      mode="add", alert_count=alert, state_dir=state_dir, dry_run=True, force=force, log=log)
        log("Dry run complete. Nothing was changed in Square.")
        _record_upload(uploads_path, fh, moxfield_csv, report, dry_run=True)
        return

    cstats = apply_catalog(client, plan, log=log)
    report.items_created = cstats["items_created"]
    report.variations_created = cstats["variations_created"]

    log("Adding stock counts ...")
    inv = run_inventory(rows_out, client=client, location_name=str(s.get("square.location_name", "") or ""),
                        mode="add", alert_count=alert, state_dir=state_dir, dry_run=False, force=force, log=log)
    report.inventory_applied = inv.applied
    report.errors.extend(inv.errors)
    if inv.not_in_square:
        report.warnings.append(f"{len(inv.not_in_square)} SKUs were not found in Square after the catalog "
                               f"update: {inv.not_in_square[:5]}")

    log("Attaching images to new items ...")
    img = run_images(records, cache_dir=s.path("paths.cache"), state_dir=state_dir,
                     user_agent=str(s.get("scryfall.user_agent")),
                     include_language=bool(s.get("sku.include_language", False)),
                     dry_run=False, client=client, log=lambda m: None)
    report.images_uploaded = img.uploaded
    report.errors.extend(img.errors)
    log(f"  {img.uploaded} images uploaded, {img.already_done} already had one")

    _record_upload(uploads_path, fh, moxfield_csv, report, dry_run=False)
    _write_report(out / "upload_report.md", moxfield_csv, report)
    log("Done." if report.ok else "Finished with errors.")


def _record_upload(path: Path, file_hash: str, csv_path: Path, report: UploadReport, dry_run: bool) -> None:
    ups = _uploads(path)
    ups.append({"hash": file_hash, "file": csv_path.name, "at": datetime.now().isoformat(timespec="seconds"),
                "dry_run": dry_run, "rows": report.moxfield_rows, "quantity": report.quantity_total,
                "items_created": report.items_created, "variations_created": report.variations_created,
                "ok": report.ok})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"uploads": ups[-200:]}, indent=1), encoding="utf-8")


def _write_report(path: Path, csv_path: Path, r: UploadReport) -> None:
    lines = [f"# Upload report: {csv_path.name}", "", f"At: {datetime.now().isoformat(timespec='seconds')}", "",
             "| | |", "| --- | --- |",
             f"| Moxfield rows | {r.moxfield_rows} |", f"| Unmatched | {r.unmatched} |", f"| Dropped | {r.dropped} |",
             f"| Items in file | {r.items_total} |", f"| Variations in file | {r.variations_total} |",
             f"| Cards added to stock | {r.quantity_total} |", f"| New items created | {r.items_created} |",
             f"| New variations created | {r.variations_created} |",
             f"| Existing items extended | {r.items_extended} |",
             f"| Inventory changes applied | {r.inventory_applied} |", f"| Images uploaded | {r.images_uploaded} |"]
    if r.warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in r.warnings]
    if r.errors:
        lines += ["", "## Errors", ""] + [f"- {e}" for e in r.errors]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
