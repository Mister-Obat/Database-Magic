"""Read and write personal deck lists without network or image access."""
from __future__ import annotations

import json
import re
from contextlib import closing
from pathlib import Path, PureWindowsPath

from .deck_media import _safe_name
from .list_import import _key
from .personal_decks import SECTION_LABELS, create_deck_from_text, get_deck
from .v2.query import connect


_ZONE_NAMES = {
    "commander": ("commander", "commanders", "commandant", "commandants", "command zone"),
    "mainboard": ("deck", "main", "main deck", "maindeck", "mainboard", "deck principal"),
    "sideboard": ("sideboard", "reserve", "sb"),
    "maybeboard": ("maybeboard", "maybe board", "cartes envisagees"),
    "companion": ("companion", "compagnon"),
    "tokens": ("tokens", "token", "jetons"),
    "display_commander": ("commandant de presentation", "commandants de presentation",
                          "display commander", "display commanders", "displaycommander"),
    "planar_deck": ("planar deck", "deck planaire", "planes"),
    "scheme_deck": ("scheme deck", "machinations", "schemes"),
}
_ZONE_LABELS = {**SECTION_LABELS, "companion": "Compagnon", "maybeboard": "Cartes envisagées"}
_ZONE_HEADERS = {alias: zone for zone, aliases in _ZONE_NAMES.items() for alias in aliases}
_EXPORT_HEADERS = {
    "commander": "Commander", "mainboard": "Deck", "sideboard": "Sideboard",
    "companion": "Companion", "maybeboard": "Maybeboard", "tokens": "Tokens",
    "planar_deck": "Planar Deck",
    "scheme_deck": "Scheme Deck",
}
_NAME = re.compile(r"^(?:deck\s+name\s*:\s*|name(?:\s*:\s*|\s+))(.+)$", re.I)
# A separated x must be a complete multiplier token, not the start of Xande
# or the name X followed by an edition reference.
_QUANTITY = re.compile(r"^([0-9]+)(?:[x×]\s*|\s+(?:[x×]\s+(?![\s(\[]))?)(.+)$", re.I)
_PRINTING = re.compile(r"^(.+?)\s+\(([A-Za-z0-9]{2,12})\)\s+([^\s]+)$")
_SET_ONLY = re.compile(r"^(.+?)\s+\[([A-Za-z0-9]{2,12})\]$")
_XMAGE_PRINTING = re.compile(r"^\[([A-Za-z0-9]{2,12}):([^\]\s]+)\]\s+(.+)$")
_TRAILING_TAGS = re.compile(r"(?:\s+#[\w-]+)+$")
_ARCHIDEKT_STATUS = re.compile(r"\s+\^[^\^]+\^$")
_ARCHIDEKT_ZONE = re.compile(r"\s+\[([^\[\]{}]+)(?:\{[^{}]*\})?\]$")
_MARKER = re.compile(r"\s+(\*(?:[FE]|CMDR)\*|\(F\))$", re.I)
_SECTION_COMMENT = "// Section: "
_COLORS_COMMENT = "// Colors: "
_MAX_LENGTH = 200 * 1024


def _new_section(name, zone):
    section = {"name": name, "cards": []}
    if zone:
        section["source_section"] = zone
    return section


def _parse_card(line):
    amount = _QUANTITY.fullmatch(line)
    if amount:
        raw_count, line = amount.groups()
        number = raw_count.lstrip("0")
        if not number or len(number) > 4:
            raise ValueError("Quantité hors limites (1 à 9999).")
        count = int(number)
    else:
        count = 1
    line = _TRAILING_TAGS.sub("", line.strip())
    line = _ARCHIDEKT_STATUS.sub("", line)
    zone = None
    archidekt = _ARCHIDEKT_ZONE.search(line)
    if archidekt and (zone := _ZONE_HEADERS.get(_key(archidekt.group(1)))):
        line = line[:archidekt.start()]
    while marker := _MARKER.search(line):
        if marker.group(1).upper() == "*CMDR*":
            zone = "commander"
        line = line[:marker.start()]
    printing = _PRINTING.fullmatch(line)
    xmage = _XMAGE_PRINTING.fullmatch(line)
    edition = _SET_ONLY.fullmatch(line)
    if printing:
        name, set_code, collector = printing.groups()
    elif xmage:
        set_code, collector, name = xmage.groups()
    elif edition:
        name, set_code = edition.groups()
        collector = None
    else:
        name, set_code, collector = line, None, None
    if not _key(name) or len(name) > 200 or (collector and len(collector) > 100):
        raise ValueError("Ligne de carte invalide.")
    return count, name.strip(), set_code and set_code.lower(), collector, bool(amount), zone


def _resolve(conn, name, set_code, collector):
    normalized = _key(name)
    if not normalized:
        return None
    phrase = f'"{normalized}"'
    where = ["e.eligibility = 'eligible'"]
    parameters = []
    if set_code:
        where.append("e.code IN (?, ?)")
        parameters.extend((set_code, set_code.upper()))
    if collector:
        where.append("p.collector_number COLLATE NOCASE = ?")
        parameters.append(collector)
    # A known edition and number can be looked up directly, including source
    # names with punctuation that the FTS tokenizer treats differently.
    search_join = ""
    if not collector:
        search_join = "JOIN source_search ON source_search.provider_card_id = sr.provider_card_id"
        where.append("source_search MATCH ?")
        parameters.append("{name printed_name}: " + phrase)
    rows = conn.execute(
        f"""SELECT sr.provider_card_id, sr.printing_id, sr.name, sr.printed_name,
            sr.lang, sr.type_line, sr.mana_value, sr.color_identity_json, sr.colors_key,
            p.collector_number, e.code AS set_code
            FROM source_records sr JOIN printings p USING(printing_id)
            JOIN editions e USING(set_id) {search_join}
            WHERE {' AND '.join(where)}
            ORDER BY e.released_at DESC, e.code DESC, p.collector_number,
                CASE sr.lang WHEN 'fr' THEN 0 WHEN 'en' THEN 1 ELSE 2 END,
                sr.provider_card_id""",
        parameters,
    )
    matches = []
    for row in rows:
        names = [row["name"], row["printed_name"] or ""]
        exact = any(_key(value) == normalized for value in names)
        front = any(_key(value.split(" // ", 1)[0]) == normalized for value in names)
        if exact or front:
            matches.append((row, exact))
    if not matches:
        return None
    exact_matches = [row for row, exact in matches if exact]
    candidates = exact_matches or [row for row, _ in matches]
    if collector and len({row["printing_id"] for row in candidates}) != 1:
        return None
    return dict(candidates[0])


def import_text(path, catalog_db, value, filename=""):
    """Import a browser-supplied list and retain unresolved counted cards."""
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > _MAX_LENGTH:
        raise ValueError("Liste de cartes vide ou trop longue.")
    if not isinstance(filename, str):
        raise ValueError("Nom du fichier invalide.")

    inferred_name = PureWindowsPath(filename).stem.strip()
    name = inferred_name[:200] or "Deck importé"
    sections, default_sections, issues = [], {}, []
    section = None
    active_zone = "mainboard"
    colors = set()
    explicit_colors = None
    custom_order = False
    imported = 0
    cache = {}

    def zone_section(zone):
        if zone not in default_sections:
            default_sections[zone] = _new_section(_ZONE_LABELS[zone], zone)
            sections.append(default_sections[zone])
        return default_sections[zone]

    with closing(connect(catalog_db)) as conn:
        for line_number, raw in enumerate(value.lstrip("\ufeff").splitlines(), 1):
            line = raw.strip()
            if not line:
                continue
            issue = {"line": line_number, "input": raw}
            if line.startswith(_SECTION_COMMENT):
                try:
                    label, zone = json.loads(line[len(_SECTION_COMMENT):])
                    if not isinstance(label, str) or not 0 < len(label.strip()) <= 200:
                        raise ValueError
                    if zone is not None and zone not in _ZONE_LABELS:
                        raise ValueError
                except (ValueError, TypeError):
                    issues.append({**issue, "reason": "Section invalide."})
                else:
                    section = _new_section(label.strip(), zone)
                    if zone != "display_commander":
                        sections.append(section)
                        custom_order = True
                continue
            if line.startswith(_COLORS_COMMENT.rstrip()):
                metadata = line[len(_COLORS_COMMENT.rstrip()):].strip()
                if all(color in "WUBRG" for color in metadata):
                    explicit_colors = set(metadata)
                else:
                    issues.append({**issue, "reason": "Couleurs invalides."})
                continue
            commented = line.startswith(("//", "#"))
            content = (line.lstrip("/#") if commented else line).strip()
            title = _NAME.fullmatch(content)
            if title:
                name = title.group(1).strip()
                continue
            heading = content.strip("[]").rstrip(":").strip()
            zone = _ZONE_HEADERS.get(_key(heading))
            if zone:
                active_zone = zone
                section = None
                continue
            if commented or heading.casefold() == "about":
                continue
            sideboard = re.match(r"^SB:\s*(.+)$", line, re.I)
            card_section = section
            if sideboard:
                line = sideboard.group(1)
            try:
                count, card_name, set_code, collector, has_quantity, card_zone = _parse_card(line)
            except ValueError as exc:
                issues.append({**issue, "reason": str(exc)})
                continue
            zone = ("sideboard" if sideboard else card_zone
                    or (card_section.get("source_section") if card_section is not None else active_zone))
            if zone == "display_commander":
                continue
            identity = (card_name, set_code, collector)
            if identity not in cache:
                cache[identity] = _resolve(conn, *identity)
            match = cache[identity]
            if match:
                card = dict(match)
                colors.update(json.loads(card.pop("color_identity_json")))
                card.pop("lang")
            else:
                issues.append({**issue, "reason": "Impression introuvable." if set_code else "Carte introuvable."})
                if not has_quantity:
                    continue
                card = {"name": card_name, "set_code": set_code, "collector_number": collector}
            if sideboard or card_zone:
                card_section = zone_section("sideboard" if sideboard else card_zone)
            if card_section is None:
                section = card_section = zone_section(active_zone)
            card_section["cards"].append({**card, "count": count})
            imported += count
    if not imported and not sections:
        raise ValueError("Aucune carte ou section de deck reconnue.")
    if not custom_order:
        sections.sort(key=lambda section: section.get("source_section") != "commander")
    if len(sections) > 100:
        raise ValueError("Conserver de 1 à 100 sections.")
    chosen_colors = explicit_colors if explicit_colors is not None else colors
    deck = create_deck_from_text(path, name, sorted(chosen_colors), sections)
    return {"deck": deck, "unresolved": issues, "imported": imported}


def _export_lines(deck):
    # Section comments preserve editable local names and order. Standard zone
    # headings and card lines keep the list readable by other deck tools.
    yield "About"
    yield f"Name {' '.join(deck['name'].splitlines())}"
    yield _COLORS_COMMENT + "".join(deck["colors"])
    active_zone = None
    for section in deck["sections"]:
        zone = section.get("source_section")
        export_zone = zone if zone in _EXPORT_HEADERS else "mainboard"
        yield ""
        if export_zone != active_zone:
            yield _EXPORT_HEADERS[export_zone]
            active_zone = export_zone
        yield _SECTION_COMMENT + json.dumps([section["name"], zone], ensure_ascii=False)
        for card in section["cards"]:
            name = " ".join((card.get("name") or card.get("printed_name") or "Carte").splitlines())
            set_code = card.get("set_code")
            number = card.get("collector_number")
            identity = f" ({set_code.upper()}) {number}" if set_code and number else f" [{set_code.upper()}]" if set_code else ""
            yield f"{card['count']} {name}{identity}"


def export_text(path, deck_id, desktop=None):
    deck = get_deck(path, deck_id)
    value = ("\n".join(_export_lines(deck)) + "\n").encode("utf-8")
    desktop = Path(desktop) if desktop is not None else Path.home() / "Desktop"
    desktop = desktop.expanduser().resolve()
    desktop.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(deck["name"]).splitlines()[0][:200].rstrip(" .") or "Deck"
    # Windows reserves device names even when several extensions follow them.
    if _safe_name(stem.split(".", 1)[0]) != stem.split(".", 1)[0]:
        stem = "_" + stem
    index = 1
    while True:
        suffix = "" if index == 1 else f" ({index})"
        target = desktop / f"{stem}{suffix}.txt"
        if target.is_dir():
            index += 1
            continue
        try:
            output = target.open("xb")
        except FileExistsError:
            index += 1
            continue
        try:
            with output:
                output.write(value)
        except OSError:
            target.unlink(missing_ok=True)
            raise
        break
    total = sum(card["count"] for section in deck["sections"] for card in section["cards"])
    return {"file": str(target), "total": total}
