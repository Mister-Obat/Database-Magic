import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from bibliotheque_mtg.v2.catalogue import identity_groups, import_legacy_aliases, migrate


def row(card_id, lang, *, number="1", illustration="art-1", oracle="oracle-1",
        layout="normal", finishes=None, set_id="set-1"):
    raw = {
        "name": "Forest" if lang == "en" else "Forêt",
        "printed_name": None if lang == "en" else "Forêt",
        "illustration_id": illustration,
        "finishes": finishes or ["nonfoil"],
        "layout": layout,
        "oracle_text": "Rules text",
        "card_faces": ([{"name": "Front", "image_uris": {"large": "https://example/front.jpg"}},
                         {"name": "Back", "image_uris": {"large": "https://example/back.jpg"}}]
                        if layout in {"transform", "modal_dfc", "double_faced_token", "reversible_card"}
                        else None),
    }
    return {"id": card_id, "lang": lang, "set_id": set_id, "set_code": "s1",
            "collector_number": number, "oracle_id": oracle, "layout": layout,
            "name": raw["name"], "type_line": "Basic Land — Forest", "mana_cost": None,
            "mana_value": 0, "colors_json": "[]", "color_identity_json": "[]",
            "rarity": "common", "artist_json": "[]", "printed_text": None,
            "oracle_text": "Rules text", "raw_json": json.dumps(raw, ensure_ascii=False)}


def source_database(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE sets(id TEXT PRIMARY KEY,code TEXT,name TEXT,set_type TEXT,released_at TEXT,
          digital INTEGER,card_count INTEGER,eligibility TEXT,exclusion_reason TEXT,raw_json TEXT);
        CREATE TABLE cards(id TEXT PRIMARY KEY,oracle_id TEXT,lang TEXT,set_id TEXT,set_code TEXT,
          collector_number TEXT,layout TEXT,name TEXT,type_line TEXT,mana_cost TEXT,mana_value REAL,
          colors_json TEXT,color_identity_json TEXT,rarity TEXT,artist_json TEXT,printed_text TEXT,
          oracle_text TEXT,raw_json TEXT);
        INSERT INTO sets VALUES('set-1','s1','Set One','expansion','2020-01-01',0,1,'eligible',NULL,'{}');
    """)
    records = [row("en-1", "en"), row("fr-1", "fr", finishes=["foil", "nonfoil"])]
    conn.executemany("INSERT INTO cards VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (r["id"], r["oracle_id"], r["lang"], r["set_id"], r["set_code"], r["collector_number"],
         r["layout"], r["name"], r["type_line"], r["mana_cost"], r["mana_value"], r["colors_json"],
         r["color_identity_json"], r["rarity"], r["artist_json"], r["printed_text"], r["oracle_text"], r["raw_json"])
        for r in records])
    conn.commit()
    conn.close()


class IdentityV2Tests(unittest.TestCase):
    def test_finish_difference_does_not_split_same_fr_en_printing(self):
        groups = identity_groups([row("en-1", "en"), row("fr-1", "fr", finishes=["foil", "nonfoil"])])
        self.assertEqual(1, len(groups))
        self.assertEqual("confirmed_multilang", groups[0]["status"])
        self.assertEqual({"en", "fr"}, {r["lang"] for r in groups[0]["rows"]})

    def test_exact_printing_can_hold_more_than_two_languages(self):
        groups = identity_groups([row("en-1", "en"), row("fr-1", "fr"), row("de-1", "de")])
        self.assertEqual(1, len(groups))
        self.assertEqual("confirmed_multilang", groups[0]["status"])
        self.assertEqual({"en", "fr", "de"}, {r["lang"] for r in groups[0]["rows"]})

    def test_distinct_illustration_variants_remain_separate(self):
        groups = identity_groups([row("en-1", "en"), row("en-2", "en", illustration="art-2")])
        self.assertEqual(2, len(groups))
        self.assertEqual({"single_language"}, {g["status"] for g in groups})

    def test_ambiguous_same_language_key_is_preserved_as_exceptions(self):
        groups = identity_groups([row("en-1", "en"), row("en-2", "en"), row("fr-1", "fr")])
        self.assertEqual(3, len(groups))
        self.assertTrue(all(g["status"] == "unresolved_identity" for g in groups))
        self.assertTrue(all(g["exception"] == "ambiguous_identity_key" for g in groups))

    def test_missing_identity_evidence_is_not_falsely_joined(self):
        groups = identity_groups([row("en-1", "en", oracle=None, illustration=None),
                                  row("fr-1", "fr", oracle=None, illustration=None)])
        self.assertEqual(2, len(groups))
        self.assertTrue(all(g["exception"] == "missing_set_number_or_identity_evidence" for g in groups))

    def test_oracle_and_collector_can_pair_when_illustration_is_absent(self):
        groups = identity_groups([row("en-1", "en", illustration=None),
                                  row("fr-1", "fr", illustration=None)])
        self.assertEqual(1, len(groups))
        self.assertEqual("confirmed_multilang", groups[0]["status"])

    def test_mismatched_face_structures_are_not_joined(self):
        english = row("en-1", "en", layout="modal_dfc")
        french = row("fr-1", "fr", layout="modal_dfc")
        raw = json.loads(french["raw_json"])
        raw["card_faces"] = raw["card_faces"][:1]
        french["raw_json"] = json.dumps(raw)
        groups = identity_groups([english, french])
        self.assertEqual(2, len(groups))
        self.assertTrue(all(g["status"] == "unresolved_identity" for g in groups))
        self.assertTrue(all(g["exception"] == "language_face_structure_mismatch" for g in groups))

    def test_multiface_images_are_not_counted_as_two_cards(self):
        groups = identity_groups([row("en-1", "en", layout="modal_dfc")])
        # The migration test checks face cardinality; identity grouping remains one printing.
        self.assertEqual(1, len(groups))

    def test_migration_is_reproducible_and_atomic_for_a_small_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite"
            first = root / "first.sqlite"
            second = root / "second.sqlite"
            source_database(source)
            a = migrate(source, first, archive_sha256="a" * 64, archive_as_of="2026-10-20")
            b = migrate(source, second, archive_sha256="a" * 64, archive_as_of="2026-10-20")
            # printing_added intentionally records the clock time of each import.
            with closing(sqlite3.connect(first)) as first_db, closing(sqlite3.connect(second)) as second_db:
                first_dump = [row for row in first_db.iterdump() if not row.startswith('INSERT INTO "printing_added"')]
                second_dump = [row for row in second_db.iterdump() if not row.startswith('INSERT INTO "printing_added"')]
            self.assertEqual(first_dump, second_dump)
            self.assertEqual(a["source_records"], b["source_records"])
            self.assertEqual(2, a["source_records"])
            self.assertEqual(1, a["printings"])
            self.assertEqual(2, a["search_rows"])
            conn = sqlite3.connect(first)
            self.assertEqual("ok", conn.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM printing_languages").fetchone()[0])
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM card_faces").fetchone()[0])
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM identity_exceptions").fetchone()[0])
            conn.close()

    def test_multiface_migration_creates_two_physical_face_slots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite"
            source_database(source)
            conn = sqlite3.connect(source)
            r = row("modal-1", "en", layout="modal_dfc")
            conn.execute("INSERT INTO cards VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                r["id"], r["oracle_id"], r["lang"], r["set_id"], r["set_code"], r["collector_number"],
                r["layout"], r["name"], r["type_line"], r["mana_cost"], r["mana_value"], r["colors_json"],
                r["color_identity_json"], r["rarity"], r["artist_json"], r["printed_text"], r["oracle_text"], r["raw_json"]))
            conn.commit()
            conn.close()
            result = migrate(source, root / "v2.sqlite", archive_sha256="b" * 64, archive_as_of="2026-10-20")
            conn = sqlite3.connect(root / "v2.sqlite")
            try:
                self.assertEqual(3, result["faces"])
                per_printing = [r[0] for r in conn.execute(
                    "SELECT COUNT(*) FROM card_faces GROUP BY printing_id ORDER BY COUNT(*)")]
                self.assertEqual([1, 2], per_printing)
                self.assertEqual([1], [r[0] for r in conn.execute(
                    "SELECT face_index FROM card_faces WHERE face_index > 0 ORDER BY face_index")])
            finally:
                conn.close()

    def test_legacy_alias_import_is_provider_id_based_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite"
            target = root / "v2.sqlite"
            source_database(source)
            migrate(source, target, archive_sha256="c" * 64, archive_as_of="2026-10-20")
            generation = root / "legacy"
            edition_dir = generation / "set-one"
            edition_dir.mkdir(parents=True)
            (generation / "index.json").write_text(json.dumps({
                "editions": [{"index_json": "set-one/index.json"}]}), encoding="utf-8")
            (edition_dir / "index.json").write_text(json.dumps({"cards": [{
                "printing_id": "legacy-print-1",
                "provider_ids": {"scryfall_en": "en-1", "scryfall_fr": "fr-1"},
            }]}), encoding="utf-8")
            first = import_legacy_aliases(target, generation)
            second = import_legacy_aliases(target, generation)
            self.assertEqual({"confirmed": 1}, first["status_counts"])
            self.assertEqual(first["status_counts"], second["status_counts"])
            conn = sqlite3.connect(target)
            try:
                alias = conn.execute("SELECT v2_printing_id,status FROM legacy_aliases WHERE legacy_id='legacy-print-1'").fetchone()
                self.assertEqual("confirmed", alias[1])
                self.assertTrue(alias[0].startswith("v2prn_"))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
