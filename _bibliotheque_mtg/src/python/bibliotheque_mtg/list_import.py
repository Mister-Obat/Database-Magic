"""Resolve a pasted card list against the published catalogue before downloading."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote, urlsplit

from .v2.query import connect


def _key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in value if not unicodedata.combining(c))).strip()


def parse_list(value: str) -> list[tuple[str, str | None]]:
    entries = []
    for raw in value.splitlines():
        line = raw.strip().lstrip("-* ").strip()
        if not line or line.startswith("#"):
            continue
        link = re.fullmatch(r"\[([^]]+)\]\((https?://[^)]+)\)", line)
        if link:
            line = link.group(2)
        if line.lower().startswith(("http://", "https://")):
            url = urlsplit(line)
            if url.scheme != "https" or url.hostname not in {"edhrec.com", "www.edhrec.com"}:
                raise ValueError(f"URL EDHREC HTTPS attendue : {line[:100]}")
            parts = [unquote(part) for part in url.path.strip("/").split("/")]
            if len(parts) != 2 or parts[0] != "cards" or not parts[1]:
                raise ValueError(f"Lien de carte EDHREC invalide : {line[:100]}")
            entries.append((parts[1].replace("-", " "), line))
        else:
            entries.append((line, None))
    if not entries or len(entries) > 100:
        raise ValueError("Saisissez entre 1 et 100 cartes (une par ligne).")
    if any(not _key(name) for name, _ in entries):
        raise ValueError("Chaque ligne doit contenir un nom de carte valide.")
    return entries


def resolve_list(db, value: str) -> list[dict]:
    entries = parse_list(value)
    results = []
    with connect(db) as conn:
        for name, url in entries:
            query = " AND ".join('"' + token + '"' for token in _key(name).split())
            matches = [dict(row) for row in conn.execute(
                "SELECT sr.provider_card_id,sr.printing_id,sr.name,sr.printed_name,sr.lang,"
                "p.collector_number,e.code AS set_code,e.name AS edition,e.released_at "
                "FROM source_search JOIN source_records sr ON sr.provider_card_id=source_search.provider_card_id "
                "JOIN printings p USING(printing_id) JOIN editions e USING(set_id) "
                "WHERE source_search MATCH ? AND e.eligibility='eligible' AND sr.lang IN ('fr','en')", (query,))
                if _key(row["name"]) == _key(name) or _key(row["printed_name"] or "") == _key(name)]
            if not matches:
                results.append({"input": name, "url": url, "status": "not_found"})
                continue
            # A bare name has no edition: choose the newest catalogued printing,
            # preferring a French source record within that printing.
            matches.sort(key=lambda row: (row["released_at"] or "", row["set_code"], row["collector_number"]), reverse=True)
            first = matches[0]
            same_printing = [row for row in matches if row["printing_id"] == first["printing_id"]]
            chosen = next((row for row in same_printing if row["lang"] == "fr"), first)
            results.append({"input": name, "url": url, "status": "matched", **chosen})
    return results
