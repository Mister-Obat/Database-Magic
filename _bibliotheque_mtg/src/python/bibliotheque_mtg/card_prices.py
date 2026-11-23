"""Cardmarket estimates keyed by the canonical card name."""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

import requests

from .maintenance import MaintenanceCancelled
from .v2.query import connect


_PRICE_GUIDE = "https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_1.json"
_MAX_GUIDE_BYTES = 64 * 1024 * 1024


def _database(library_dir: Path) -> Path:
    return library_dir / "state" / "card-prices.sqlite"


def read_card_prices(library_dir: Path) -> dict[str, int]:
    database = _database(library_dir)
    if not database.exists():
        return {}
    with closing(connect(database)) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='card_prices'").fetchone()
        return dict(conn.execute("SELECT name, cents FROM card_prices")) if exists else {}


def _cached_guide(library_dir: Path, version_id: str) -> tuple[str, str, int] | None:
    database = _database(library_dir)
    if not database.exists():
        return None
    with closing(connect(database)) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='price_guide'").fetchone()
        if not exists:
            return None
        row = conn.execute(
            "SELECT etag, created_at, (SELECT COUNT(*) FROM card_prices) "
            "FROM price_guide WHERE catalogue_version = ?", (version_id,)).fetchone()
        return tuple(row) if row and row[0] and row[2] else None


def _as_cents(value) -> int | None:
    if type(value) in {float, int} and math.isfinite(value) and value > 0:
        return round(value * 100) or None
    return None


def _product_prices(rows: list, check_cancel) -> dict[int, tuple[int | None, int | None]]:
    prices = {}
    for index, row in enumerate(rows):
        if index % 1000 == 0:
            check_cancel()
        product = row.get("idProduct")
        if type(product) is int and product > 0:
            regular = _as_cents(row.get("trend"))
            foil = _as_cents(row.get("trend-foil"))
            if regular is not None or foil is not None:
                prices[product] = (regular, foil)
    return prices


def _group_by_name(catalogue_db: Path, products: dict, check_cancel) -> dict[str, int]:
    regular_prices: dict[str, int] = {}
    foil_prices: dict[str, int] = {}
    with closing(connect(catalogue_db)) as conn:
        # A progress handler interrupts SQLite even while it is reading records with no product ID.
        cancelled = False

        def interrupt() -> int:
            nonlocal cancelled
            try:
                check_cancel()
            except MaintenanceCancelled:
                cancelled = True
            return int(cancelled)

        conn.set_progress_handler(interrupt, 5000)
        try:
            records = conn.execute(
                """SELECT name, cardmarket_id FROM (
                       SELECT name, json_extract(raw_json, '$.cardmarket_id') AS cardmarket_id
                       FROM source_records
                   ) WHERE cardmarket_id IS NOT NULL AND name <> ''""")
            for name, product_id in records:
                match = products.get(product_id)
                if match:
                    for target, cents in zip((regular_prices, foil_prices), match):
                        if cents is not None:
                            target[name] = min(cents, target.get(name, cents))
        except sqlite3.OperationalError:
            if cancelled:
                check_cancel()
            raise
    return foil_prices | regular_prices


def _save_prices(database: Path, prices: dict[str, int], *, etag: str, created_at: str,
                 version_id: str, check_cancel) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS card_prices (name TEXT PRIMARY KEY, "
            "cents INTEGER NOT NULL CHECK(cents > 0)) WITHOUT ROWID")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS price_guide (etag TEXT NOT NULL, created_at TEXT NOT NULL, "
            "catalogue_version TEXT NOT NULL)")
        conn.execute("DELETE FROM card_prices")
        conn.execute("DELETE FROM price_guide")
        conn.executemany("INSERT INTO card_prices VALUES (?, ?)", prices.items())
        conn.execute("INSERT INTO price_guide VALUES (?, ?, ?)", (etag, created_at, version_id))
        check_cancel()


def refresh_card_prices(library_dir: Path, current: dict, *, progress, cancel_event) -> dict:
    """Download one official guide, then atomically replace the global local estimates."""
    def check_cancel() -> None:
        if cancel_event.is_set():
            raise MaintenanceCancelled("Mise à jour annulée")

    check_cancel()
    version_id = current["version_id"]
    cached = _cached_guide(library_dir, version_id)
    headers = {"If-None-Match": cached[0]} if cached else {}
    with requests.get(_PRICE_GUIDE, headers=headers, stream=True, timeout=(10, 30)) as response:
        if response.status_code == 304 and cached:
            check_cancel()
            return {"status": "no_change", "as_of": cached[1], "cards": cached[2]}
        if response.status_code != 200:
            response.raise_for_status()
            raise ValueError("Réponse inattendue du guide Cardmarket.")
        body = bytearray()
        size = int(response.headers.get("Content-Length") or 0)
        if size > _MAX_GUIDE_BYTES:
            raise ValueError("Le guide Cardmarket dépasse la limite de 64 Mio.")
        for chunk in response.iter_content(chunk_size=256 * 1024):
            check_cancel()
            body.extend(chunk)
            if len(body) > _MAX_GUIDE_BYTES:
                raise ValueError("Le guide Cardmarket dépasse la limite de 64 Mio.")
            if size:
                progress(min(len(body) / size, 1) * 0.7, "Téléchargement du guide Cardmarket…")
        etag = response.headers.get("ETag", "")
    guide = json.loads(body)
    del body
    if not isinstance(guide, dict) or not isinstance(guide.get("priceGuides"), list):
        raise ValueError("Format du guide Cardmarket inattendu.")
    created_at = guide.get("createdAt")
    if not isinstance(created_at, str) or not created_at:
        raise ValueError("Date du guide Cardmarket manquante.")
    products = _product_prices(guide["priceGuides"], check_cancel)
    del guide
    progress(0.75, "Regroupement des prix Cardmarket par nom de carte…")
    prices = _group_by_name(current["database"], products, check_cancel)
    if not prices:
        raise ValueError("Aucun prix Cardmarket associé au catalogue local.")
    progress(0.95, "Enregistrement des prix en euros…")
    _save_prices(_database(library_dir), prices, etag=etag, created_at=created_at,
                 version_id=version_id, check_cancel=check_cancel)
    return {"status": "updated", "as_of": created_at, "cards": len(prices)}
