import tempfile
import unittest
import sqlite3
import os
from pathlib import Path

from bibliotheque_mtg.v2.catalogue import migrate
from bibliotheque_mtg.v2.query import _french_type_aliases, _match_term, card_detail, edition_cards, list_editions, search_cards
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

    def test_accent_and_filters_return_expected_provider_record(self):
        result=search_cards(self.db,'Forêt',set_code='s1',language='fr',limit=50)
        self.assertEqual(1,result['total'])
        self.assertEqual('fr-1',result['items'][0]['provider_card_id'])

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
        with self.assertRaises(ValueError):
            search_cards(self.db,color='CW')

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

    def test_edition_cards_show_one_french_record_per_oracle_before_pagination(self):
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
        self.assertEqual(1, result['total'])
        self.assertEqual('fr-1', result['items'][0]['provider_card_id'])

        conn = sqlite3.connect(self.db)
        try:
            conn.execute("DELETE FROM source_records WHERE provider_card_id='fr-1'")
            conn.commit()
        finally:
            conn.close()
        result = edition_cards(self.db, 's1')
        self.assertEqual(1, result['total'])
        self.assertEqual('en', result['items'][0]['lang'])

    def test_edition_and_search_pages_allow_300(self):
        self.assertEqual(300, edition_cards(self.db, 's1', limit=300)['limit'])
        self.assertEqual(300, edition_cards(self.db, 's1', limit=1000)['limit'])
        self.assertEqual(300, search_cards(self.db, limit=300)['limit'])

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
