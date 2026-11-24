import tempfile
import unittest
import json
import sqlite3
import os
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

from bibliotheque_mtg.proxies import Proxies
from bibliotheque_mtg.v2 import api
from bibliotheque_mtg.v2.catalogue import migrate
from bibliotheque_mtg.v2.query import _french_type_aliases, _match_term, card_detail, card_legalities, edition_cards, list_editions, search_cards
from .test_catalogue_v2 import source_database


class QueryV2Tests(unittest.TestCase):
    def test_french_token_alias_keeps_the_original_term(self):
        self.assertEqual('("indice"* OR "clue"*)', _match_term('indice'))
        self.assertEqual('("Trésor"* OR "treasure"*)', _match_term('Trésor'))
        self.assertEqual('("indice"* OR "clue"*) AND "rare"*', _match_term('indice rare'))

    def test_french_subtype_alias_comes_from_bilingual_printing(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE source_records SET type_line='Creature — Squirrel', "
                         "printed_type_line='Créature — écureuil' WHERE lang='fr'")
            conn.execute("UPDATE source_records SET type_line='Creature — Squirrel' WHERE lang='en'")
            conn.execute("UPDATE source_search SET type_line='Creature — Squirrel', "
                         "printed_type_line='Créature — écureuil' WHERE lang='fr'")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(('squirrel',), _french_type_aliases(self.db, 'écureuil'))
        self.assertIn('"squirrel"*', _match_term('écureuil', self.db))

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name)
        source=root/'source.sqlite'; self.db=root/'catalogue.sqlite'
        source_database(source)
        migrate(source,self.db,archive_sha256='d'*64,archive_as_of='2026-10-20')

    def tearDown(self):
        self.temp.cleanup()

    def test_card_legalities_preserve_distinct_statuses_and_translation_fallback(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            printing_id = conn.execute(
                "SELECT printing_id FROM source_records WHERE provider_card_id='en-1'"
            ).fetchone()[0]
            conn.executemany(
                "UPDATE source_records SET raw_json=? WHERE provider_card_id=?",
                [(' {"legalities":{"commander":"banned","modern":"not_legal","vintage":"restricted","other":true}}', 'en-1'),
                 (' {"legalities":{"commander":"legal","legacy":"legal"}}', 'fr-1')],
            )
        self.assertEqual({printing_id: {"commander": "banned", "modern": "not_legal",
                                       "vintage": "restricted", "legacy": "legal"}},
                         card_legalities(self.db, [printing_id, printing_id, "missing"]))

    def test_card_legalities_apply_new_bans_and_remove_old_ones_for_same_archive(self):
        root = self.db.parent
        version = root / "versions" / "current-example"
        version.mkdir(parents=True)
        self.db = self.db.replace(version / "catalogue.sqlite")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            printing_id, oracle_id = conn.execute(
                "SELECT printing_id, oracle_id FROM source_records JOIN printings USING(printing_id) "
                "WHERE provider_card_id='en-1'"
            ).fetchone()
            conn.execute("UPDATE source_records SET raw_json=? WHERE printing_id=?",
                         (json.dumps({"legalities": {"commander": "banned", "vintage": "legal"}}), printing_id))
        path = root / "card-restrictions.json"
        snapshot = {"archive_sha256": "d" * 64, "formats": ["commander", "vintage"],
                    "cards": {oracle_id: {"vintage": "restricted"}}}
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        self.assertEqual({printing_id: {"vintage": "restricted"}}, card_legalities(self.db, [printing_id]))
        snapshot["archive_sha256"] = "old"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        self.assertEqual({printing_id: {"commander": "banned", "vintage": "legal"}},
                         card_legalities(self.db, [printing_id]))

    def test_search_merges_proxies_by_display_name_before_pagination(self):
        root = Path(self.temp.name)
        proxies = Proxies(root / "proxy", root / "state")
        proxies.root.mkdir()
        for name in ("Alouette.png", "Baleine.png", "Zèbre.png"):
            (proxies.root / name).touch()
        identifiers = {path.stem: identifier for identifier, path in proxies.files().items()}
        proxies.associate(self.db, identifiers["Baleine"], "fr-1")

        def page(sort, offset=0, limit=1, **filters):
            return proxies.dispatch(self.db, "/api/v2/search",
                                    {"result_sort": sort, "offset": offset, "limit": limit, **filters},
                                    lambda _db, result: result)

        def ids(result):
            self.assertEqual(4, result["total"])
            return [item["provider_card_id"] for item in result["items"]]

        self.assertEqual([identifiers["Alouette"], "fr-1", identifiers["Baleine"], identifiers["Zèbre"]],
                         [ids(page("name_asc", offset))[0] for offset in range(4)])
        self.assertEqual([identifiers["Zèbre"], "fr-1", identifiers["Baleine"], identifiers["Alouette"]],
                         ids(page("name_desc", limit=4)))
        self.assertEqual([], ids(page("name_asc", offset=20)))
        self.assertEqual(["fr-1", identifiers["Baleine"]], ids(page("mana_asc", limit=2)))
        def unchanged(_db, result):
            return result

        search = proxies.start_search(self.db, {"result_sort": "name_asc", "limit": 1}, unchanged)
        self.assertEqual([identifiers["Alouette"]], ids(search))
        missing = proxies.start_search(self.db, {"image_filter": "without", "limit": 1}, unchanged)
        self.assertEqual(["fr-1"], [item["provider_card_id"] for item in missing["items"]])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE printing_added SET added_at='2999-12-31T00:00:00Z'")
            conn.execute("INSERT INTO image_assets VALUES('asset','card.webp','image/webp',1,531,738,'en','{}')")
            conn.execute("""INSERT INTO image_slots
                SELECT printing_id,face_index,'fr','en','ready','asset','{}' FROM card_faces""")
        official_image = root / "card.webp"
        official_image.touch()
        os.utime(official_image, (1000, 1000))
        self.assertEqual(["fr-1"], ids(page("added_desc")))
        with patch("bibliotheque_mtg.v2.api.search_cards", side_effect=AssertionError("Search ran again")):
            next_page = proxies.search_page(self.db, {"snapshot": search["snapshot"], "offset": 1, "limit": 1}, unchanged)
            self.assertEqual(["fr-1"], ids(next_page))
            still_missing = proxies.search_page(self.db, {"snapshot": missing["snapshot"]}, unchanged)
            self.assertEqual(["fr-1"], [item["provider_card_id"] for item in still_missing["items"]])
        self.assertEqual([], page("added_desc", image_filter="without")["items"])
        self.assertEqual(["fr-1"], ids(page("added_asc", image_filter="with")))
        for sort in ("date", "date_asc"):
            self.assertEqual(["fr-1"], ids(page(sort, offset=3)))

    def test_accent_and_filters_return_expected_provider_record(self):
        result=search_cards(self.db,'Forêt',set_code='s1',language='fr',limit=50)
        self.assertEqual(1,result['total'])
        self.assertEqual('fr-1',result['items'][0]['provider_card_id'])

    def test_multiple_choices_and_empty_selection(self):
        self.assertEqual(search_cards(self.db)['total'], search_cards(self.db, set_code='s1|s2')['total'])
        self.assertEqual(0, search_cards(self.db, set_code='-')['total'])
        self.assertEqual(0, search_cards(self.db, rarity='-')['total'])
        self.assertEqual(0, search_cards(self.db, card_type='-')['total'])
        self.assertEqual(search_cards(self.db, card_type='land')['total'],
                         search_cards(self.db, card_type='land|artifact')['total'])

    def test_phyrexian_printed_name_uses_readable_canonical_name(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("DELETE FROM source_records WHERE provider_card_id='fr-1'")
            conn.execute("DELETE FROM source_search WHERE provider_card_id='fr-1'")
            conn.execute("DELETE FROM printing_languages WHERE provider_card_id='fr-1'")
            conn.execute("UPDATE source_records SET lang='ph', printed_name='|Ducg,norn', "
                         "printed_type_line='|MewkfuDs', printed_text='|gOcdrd' WHERE provider_card_id='en-1'")
            conn.execute("UPDATE source_search SET lang='ph', printed_name='|Ducg,norn', "
                         "printed_type_line='|MewkfuDs', printed_text='|gOcdrd' WHERE provider_card_id='en-1'")
            conn.commit()
        finally:
            conn.close()
        card = card_detail(self.db, 'en-1')['card']
        self.assertIsNone(card['printed_name'])
        self.assertIsNone(card['printed_type_line'])
        self.assertIsNone(card['printed_text'])
        self.assertEqual(card['oracle_text'], card['display_text']['value'])
        self.assertIsNone(next(t['printed_name'] for t in card['translations'] if t['lang'] == 'ph'))
        self.assertIsNone(search_cards(self.db, 'Forest', language='ph')['items'][0]['printed_name'])
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual('|Ducg,norn', conn.execute("SELECT printed_name FROM source_records WHERE provider_card_id='en-1'").fetchone()[0])
        finally:
            conn.close()

    def test_partial_french_and_english_text_match_with_edition_and_display_language(self):
        for term in ('Forê', 'Fores', 'Rule'):
            result=search_cards(self.db,term,set_code='s1',language='fr')
            self.assertEqual(1,result['total'],term)
            self.assertEqual('fr-1',result['items'][0]['provider_card_id'])

    def test_secondary_ranges_and_type_are_combined_before_pagination(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE source_records SET type_line='Creature — Elf', mana_value=3, "
                         "raw_json=json_set(raw_json,'$.power','2','$.toughness','4')")
            conn.execute("UPDATE source_search SET type_line='Creature — Elf'")
            conn.commit()
        finally:
            conn.close()
        chosen = search_cards(self.db, 'Rule', card_type='creature', mana_min=2,
                              mana_max=4, power_min=2, power_max=3,
                              toughness_min=4, toughness_max=5, limit=1)
        self.assertEqual(1, chosen['total'])
        self.assertEqual(0, search_cards(self.db, 'Rule', power_min=3)['total'])
        self.assertEqual(0, search_cards(self.db, 'Rule', card_type='land')['total'])

    def test_unchecked_token_excludes_artifact_treasure(self):
        conn = sqlite3.connect(self.db)
        try:
            original = conn.execute("SELECT printing_id FROM printings LIMIT 1").fetchone()[0]
            conn.execute("INSERT INTO oracle_cards VALUES('treasure-oracle',1)")
            conn.execute("INSERT INTO printings SELECT 'treasure-printing',set_id,'99','treasure-oracle','token',illustration_id,identity_status,identity_evidence,variant_attributes_json FROM printings WHERE printing_id=?", (original,))
            record = list(conn.execute("SELECT * FROM source_records WHERE provider_card_id='en-1'").fetchone())
            record[0], record[1], record[4], record[6] = 'treasure-card', 'treasure-printing', 'Treasure Token', 'Token Artifact — Treasure'
            conn.execute("INSERT INTO source_records VALUES(" + ",".join("?" for _ in record) + ")", record)
            conn.execute("INSERT INTO source_search SELECT 'treasure-card',set_code,lang,'Treasure Token',printed_name,'Token Artifact — Treasure',printed_type_line,oracle_text,printed_text,artist FROM source_search WHERE provider_card_id='en-1'")
            conn.commit()
        finally:
            conn.close()
        all_but_token = 'creature|land|instant|sorcery|artifact|enchantment|planeswalker|battle|kindred|plane|phenomenon|scheme|conspiracy|vanguard|dungeon'
        self.assertEqual(1, search_cards(self.db, 'Treasure')['total'])
        self.assertEqual(0, search_cards(self.db, 'Treasure', card_type=all_but_token)['total'])
        self.assertEqual(0, search_cards(self.db, 'Treasure', card_type='artifact')['total'])
        self.assertEqual(1, search_cards(self.db, 'Treasure', card_type='token')['total'])

    def test_multiple_colors_and_colorless_are_distinct(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE source_records SET colors_key='UW' WHERE provider_card_id='fr-1'")
            conn.execute("UPDATE source_records SET colors_key='W' WHERE provider_card_id='en-1'")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(['fr-1'], [item['provider_card_id'] for item in search_cards(self.db,color='WU')['items']])
        self.assertEqual(['fr-1'], [item['provider_card_id'] for item in search_cards(self.db,color='WU',color_mode='exact')['items']])
        self.assertEqual(['fr-1'], [item['provider_card_id'] for item in search_cards(self.db,color='W',color_mode='exact')['items']])
        self.assertEqual(['fr-1'], [item['provider_card_id'] for item in search_cards(self.db,color='W')['items']])
        self.assertEqual(0, search_cards(self.db,color='WR')['total'])
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE source_records SET colors_key='' WHERE provider_card_id='en-1'")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(['fr-1'], [item['provider_card_id'] for item in search_cards(self.db,color='C')['items']])
        self.assertEqual(['fr-1'], [item['provider_card_id'] for item in search_cards(self.db,color='C',color_mode='exact')['items']])
        self.assertEqual(['fr-1'], [item['provider_card_id'] for item in search_cards(self.db,color='CW')['items']])
        self.assertEqual(['fr-1'], [item['provider_card_id'] for item in search_cards(self.db,color='CW',color_mode='exact')['items']])

    def test_color_mode_is_validated_without_color_selection(self):
        with self.assertRaises(ValueError):
            search_cards(self.db,color_mode='invalid')
        self.assertEqual(search_cards(self.db)['total'], search_cards(self.db,color_mode='exact')['total'])

    def test_fts_syntax_is_quoted_and_page_size_is_bounded(self):
        result=search_cards(self.db,'" OR *',limit=10000)
        self.assertEqual(300,result['limit'])
        self.assertEqual(0,result['total'])

    def test_card_detail_explicitly_marks_english_fallback(self):
        result=card_detail(self.db,'fr-1')
        self.assertTrue(result['found'])
        self.assertTrue(result['card']['display_text']['fallback_used'])
        self.assertEqual('en',result['card']['display_text']['language'])
        self.assertEqual('Rules text',result['card']['display_text']['value'])

    def test_edition_list_is_paged_and_versioned(self):
        result=list_editions(self.db,limit=1,offset=0)
        self.assertEqual('2.0.0',result['contract_version'])
        self.assertEqual(1,result['limit'])
        self.assertEqual(1,result['total'])
        edition = result['items'][0]
        self.assertEqual(1, edition['card_total'])
        self.assertEqual(0, edition['downloaded'])

    def test_edition_name_search_and_grouped_codes_find_catalogue_cards(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("INSERT INTO editions VALUES(?,?,?,?,?,?,?,?,?)",
                         ('set-2', 's2', 'Set One Commander', 'commander', '2020-01-02', 0, 1, 'eligible', None))
            conn.execute("INSERT INTO oracle_cards VALUES('commander-oracle',1)")
            conn.execute("INSERT INTO printings SELECT ?,?,collector_number,?,layout,illustration_id,"
                         "identity_status,identity_evidence,variant_attributes_json FROM printings LIMIT 1",
                         ('commander-printing', 'set-2', 'commander-oracle'))
            original = list(conn.execute("SELECT * FROM source_records WHERE provider_card_id='en-1'").fetchone())
            original[0], original[1] = 'commander-card', 'commander-printing'
            conn.execute("INSERT INTO source_records VALUES(" + ",".join("?" for _ in original) + ")", original)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(2, edition_cards(self.db, 's1,s2')['total'])
        self.assertEqual(2, search_cards(self.db, set_code='s1,s2')['total'])
        self.assertEqual(1, search_cards(self.db, 'Set One')['total'])

    def test_occurrences_by_oracle_and_latest_unique_printing(self):
        conn = sqlite3.connect(self.db)
        try:
            first = conn.execute("SELECT printing_id FROM printings").fetchone()[0]
            conn.execute("INSERT INTO editions VALUES(?,?,?,?,?,?,?,?,?)",
                         ('set-2', 's2', 'Set Two', 'expansion', '2025-01-01', 0, 1, 'eligible', None))
            conn.execute("INSERT INTO printings SELECT 'latest-printing','set-2','7',oracle_id,layout,illustration_id,identity_status,identity_evidence,variant_attributes_json FROM printings WHERE printing_id=?", (first,))
            record = list(conn.execute("SELECT * FROM source_records WHERE provider_card_id='en-1'").fetchone())
            record[0], record[1] = 'latest-card', 'latest-printing'
            conn.execute("INSERT INTO source_records VALUES(" + ",".join("?" for _ in record) + ")", record)
            conn.execute("INSERT INTO oracle_cards VALUES('other-oracle',1)")
            conn.execute("INSERT INTO printings SELECT 'homonym-printing',set_id,'8','other-oracle',layout,illustration_id,identity_status,identity_evidence,variant_attributes_json FROM printings WHERE printing_id=?", (first,))
            record[0], record[1] = 'homonym-card', 'homonym-printing'
            conn.execute("INSERT INTO source_records VALUES(" + ",".join("?" for _ in record) + ")", record)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual('oracle-1', card_detail(self.db, 'en-1')['card']['oracle_id'])
        occurrences = search_cards(self.db, 'Forest', oracle_id='oracle-1', color='C', color_mode='exact', card_type='land')
        self.assertEqual(2, occurrences['total'])
        self.assertEqual({'latest-printing', first}, {item['printing_id'] for item in occurrences['items']})
        self.assertEqual(1, search_cards(self.db, oracle_id='other-oracle')['total'])
        unique = search_cards(self.db, unique=True)
        self.assertEqual(1, unique['total'])
        self.assertEqual('latest-printing', unique['items'][0]['printing_id'])
        self.assertEqual(1, search_cards(self.db, unique='1', limit=1, offset=1)['total'])
        self.assertEqual([], search_cards(self.db, unique='1', limit=1, offset=1)['items'])

    def test_edition_cards_show_one_language_per_printing_before_pagination(self):
        result = edition_cards(self.db, 's1', limit=1)
        self.assertEqual(1, result['total'])
        self.assertEqual(['fr-1'], [item['provider_card_id'] for item in result['items']])
        self.assertEqual([], edition_cards(self.db, 's1', limit=1, offset=1)['items'])

        conn = sqlite3.connect(self.db)
        try:
            original = conn.execute("SELECT * FROM source_records WHERE provider_card_id='en-1'").fetchone()
            conn.execute("INSERT INTO printings SELECT ?,set_id,?,oracle_id,layout,illustration_id,"
                         "identity_status,identity_evidence,variant_attributes_json "
                         "FROM printings WHERE printing_id=(SELECT printing_id FROM source_records WHERE provider_card_id='en-1')",
                         ('variant-printing', '2'))
            variant = list(original)
            variant[0], variant[1] = 'en-variant', 'variant-printing'
            conn.execute("INSERT INTO source_records VALUES (" + ",".join("?" for _ in variant) + ")", variant)
            conn.commit()
        finally:
            conn.close()
        result = edition_cards(self.db, 's1')
        self.assertEqual(2, result['total'])
        self.assertEqual('fr-1', result['items'][0]['provider_card_id'])
        self.assertEqual('en-variant', result['items'][1]['provider_card_id'])
        focused = api.dispatch(self.db, '/api/v2/edition', {
            'set': 's1', 'limit': 1, 'focus': 'variant-printing',
        })
        self.assertEqual(1, focused['offset'])
        self.assertEqual(['en-variant'], [item['provider_card_id'] for item in focused['items']])

        conn = sqlite3.connect(self.db)
        try:
            conn.execute("DELETE FROM source_records WHERE provider_card_id='fr-1'")
            conn.commit()
        finally:
            conn.close()
        result = edition_cards(self.db, 's1')
        self.assertEqual(2, result['total'])
        self.assertEqual('en', result['items'][0]['lang'])

    def test_edition_and_search_pages_allow_300(self):
        self.assertEqual(300, edition_cards(self.db, 's1', limit=300)['limit'])
        self.assertEqual(300, edition_cards(self.db, 's1', limit=1000)['limit'])
        self.assertEqual(300, search_cards(self.db, limit=300)['limit'])

    def test_edition_keeps_phyrexian_number_sharing_an_oracle_with_english(self):
        conn = sqlite3.connect(self.db)
        try:
            original = list(conn.execute("SELECT * FROM source_records WHERE provider_card_id='en-1'").fetchone())
            conn.execute("INSERT INTO printings SELECT ?,set_id,?,oracle_id,layout,illustration_id,"
                         "identity_status,identity_evidence,variant_attributes_json FROM printings LIMIT 1",
                         ('phyrexian-printing', '213'))
            original[0], original[1], original[2] = 'ph-213', 'phyrexian-printing', 'ph'
            original[5] = None
            conn.execute("INSERT INTO source_records VALUES (" + ",".join("?" for _ in original) + ")", original)
            conn.commit()
        finally:
            conn.close()
        result = edition_cards(self.db, 's1')
        self.assertEqual(2, result['total'])
        self.assertEqual({'1', '213'}, {item['collector_number'] for item in result['items']})
        self.assertEqual('ph-213', next(item['provider_card_id'] for item in result['items']
                                        if item['collector_number'] == '213'))

    def test_published_search_reuses_candidates_after_an_image_write(self):
        published = self.db.parent / "versions" / "v2-0123456789ab" / "catalogue.sqlite"
        published.parent.mkdir(parents=True)
        self.db.replace(published)
        self.assertEqual(1, search_cards(published, "Forest", image_filter="without")["total"])
        with closing(sqlite3.connect(published)) as conn, conn:
            conn.execute("INSERT INTO image_assets VALUES(?,?,?,?,?,?,?,?)",
                         ("asset", "card.webp", "image/webp", 7, 531, 738, "en", "{}"))
            conn.execute("""INSERT INTO image_slots
                SELECT printing_id,face_index,'fr','en','ready','asset','{}' FROM card_faces""")
        with patch("bibliotheque_mtg.v2.query._extra_matches", side_effect=AssertionError("Cache miss")):
            self.assertEqual(1, search_cards(published, "Forest", image_filter="with")["total"])
            self.assertEqual(0, search_cards(published, "Forest", image_filter="without")["total"])

    def test_image_filter_and_added_order_use_local_webp_dates(self):
        conn = sqlite3.connect(self.db)
        try:
            first = conn.execute("SELECT printing_id FROM printings").fetchone()[0]
            conn.execute("INSERT INTO oracle_cards VALUES('other-oracle',1)")
            conn.execute("INSERT INTO printings SELECT 'other-printing',set_id,'2','other-oracle',layout,illustration_id,identity_status,identity_evidence,variant_attributes_json FROM printings WHERE printing_id=?", (first,))
            conn.execute("INSERT INTO printing_added VALUES('other-printing','2026-11-18T00:00:00Z')")
            record = list(conn.execute("SELECT * FROM source_records WHERE provider_card_id='en-1'").fetchone())
            record[0], record[1] = 'other-card', 'other-printing'
            conn.execute("INSERT INTO source_records VALUES(" + ",".join("?" for _ in record) + ")", record)
            conn.execute("INSERT INTO source_search SELECT 'other-card',set_code,lang,name,printed_name,type_line,printed_type_line,oracle_text,printed_text,artist FROM source_search WHERE provider_card_id='en-1'")
            conn.execute("INSERT INTO card_faces SELECT 'other-printing',face_index,physical_image FROM card_faces WHERE printing_id=?", (first,))
            conn.execute("INSERT INTO image_slots SELECT printing_id,face_index,'fr',NULL,'missing',NULL,'{}' FROM card_faces WHERE printing_id=?", (first,))
            conn.execute("INSERT INTO image_slots SELECT printing_id,face_index,'fr',NULL,'missing',NULL,'{}' FROM card_faces WHERE printing_id='other-printing'")
            conn.execute("UPDATE printing_added SET added_at='2026-11-17T00:00:00Z' WHERE printing_id=?", (first,))
            for printing_id, name, timestamp in ((first, 'old.webp', 1000), ('other-printing', 'new.webp', 2000)):
                image = self.db.parent / name
                image.write_bytes(b'fixture')
                os.utime(image, (timestamp, timestamp))
                conn.execute("INSERT INTO image_assets VALUES(?,?,?,?,?,?,?,?)", (name, name, 'image/webp', 7, 531, 738, 'en', '{}'))
                conn.execute("UPDATE image_slots SET status='ready',asset_id=? WHERE printing_id=?", (name, printing_id))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(0, search_cards(self.db, image_filter='without')['total'])
        self.assertEqual(2, search_cards(self.db, image_filter='with')['total'])
        self.assertEqual(['other-printing', first], [item['printing_id'] for item in search_cards(self.db, image_filter='with', result_sort='added_desc')['items']])
        self.assertEqual([first, 'other-printing'], [item['printing_id'] for item in search_cards(self.db, image_filter='with', result_sort='added_asc')['items']])
        self.assertEqual(['other-printing', first], [item['printing_id'] for item in search_cards(self.db, result_sort='added_desc')['items']])
        # The pre-tracking catalogue has a shared addition timestamp.
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE printing_added SET added_at='2026-11-18T00:00:00Z'")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(['other-printing', first], [item['printing_id'] for item in search_cards(self.db, result_sort='added_desc')['items']])
        self.assertEqual([first, 'other-printing'], [item['printing_id'] for item in search_cards(self.db, result_sort='added_asc')['items']])
        self.assertEqual(['other-printing', first], [item['printing_id'] for item in search_cards(self.db, 'Forest', result_sort='added_desc')['items']])
        self.assertEqual([first, 'other-printing'], [item['printing_id'] for item in search_cards(self.db, 'Forest', result_sort='added_asc')['items']])
        with self.assertRaises(ValueError):
            search_cards(self.db, image_filter='invalid')

    def test_search_prefers_french_even_when_english_text_matches(self):
        result = search_cards(self.db, 'Rules text')
        self.assertEqual(1, result['total'])
        self.assertEqual('fr-1', result['items'][0]['provider_card_id'])

    def test_japanese_is_used_when_french_and_english_are_absent(self):
        conn = sqlite3.connect(self.db)
        try:
            record = list(conn.execute("SELECT * FROM source_records WHERE provider_card_id='en-1'").fetchone())
            record[0], record[2] = 'ja-1', 'ja'
            conn.execute("INSERT INTO source_records VALUES (" + ",".join("?" for _ in record) + ")", record)
            conn.execute("DELETE FROM source_records WHERE lang IN ('fr','en')")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual('ja-1', search_cards(self.db)['items'][0]['provider_card_id'])
        self.assertEqual('ja-1', edition_cards(self.db, 's1')['items'][0]['provider_card_id'])


if __name__=='__main__': unittest.main()
