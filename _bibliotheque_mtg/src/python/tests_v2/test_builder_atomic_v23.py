import gzip
import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from bibliotheque_mtg.maintenance import initialize_v2
from bibliotheque_mtg.v2.build_catalogue import build_catalogue
from bibliotheque_mtg.v2.publish import resolve_current


class AtomicBuildV23Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.source=self.root/'source.sqlite'
        c=sqlite3.connect(self.source)
        c.execute("CREATE TABLE sets(id TEXT PRIMARY KEY,code TEXT,name TEXT,set_type TEXT,released_at TEXT,digital INTEGER,card_count INTEGER,eligibility TEXT,exclusion_reason TEXT,raw_json TEXT)")
        c.execute("INSERT INTO sets VALUES('fixture-set','tst','Fixture Set','expansion','2026-01-01',0,1,'eligible',NULL,'{}')")
        c.commit(); c.close()
        self.record={'id':'fixture-card-en','set_id':'fixture-set','set':'tst','collector_number':'1','oracle_id':'fixture-oracle','illustration_id':'fixture-art','layout':'normal','lang':'en','name':'Fixture Card','type_line':'Creature','mana_cost':'{0}','cmc':0,'colors':[],'color_identity':[],'rarity':'common','oracle_text':'Fixture text','card_faces':None}
        self.archive=self.root/'bulk.jsonl.gz'
        with gzip.open(self.archive,'wt',encoding='utf-8') as f: f.write(json.dumps(self.record)+'\n')
        self.legacy=self.root/'legacy'
        edition=self.legacy/'fixture'
        edition.mkdir(parents=True)
        (self.legacy/'index.json').write_text(json.dumps({'editions':[{'index_json':'fixture/index.json'}]}),encoding='utf-8')
        (edition/'index.json').write_text(json.dumps({'cards':[{'printing_id':'legacy-fixture-print','provider_ids':{'scryfall_en':'fixture-card-en'}}]}),encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def _old_target(self):
        target=self.root/'active.sqlite'
        c=sqlite3.connect(target); c.execute('create table active(value text)'); c.execute("insert into active values('previous')"); c.commit(); c.close()
        return target

    def _build(self,target,archive=None,hook=None):
        archive=archive or self.archive
        digest=hashlib.sha256(archive.read_bytes()).hexdigest()
        return build_catalogue(self.source,archive,target,archive_sha256=digest,archive_as_of='2026-10-20T00:00:00Z',edition_root=self.root/'exports',legacy_generation=self.legacy,pre_publish_hook=hook)

    def test_first_install_publishes_a_real_catalogue(self):
        shutil.copyfile(self.source, self.root / "catalogue.sqlite")
        source = {
            "archive_path": str(self.archive),
            "archive_sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest(),
            "updated_at": "2026-10-20",
            "rows": 1,
        }
        report = {"sources": {"scryfall_all_cards": source}}
        with patch("bibliotheque_mtg.sources.run_sources", return_value=report):
            result = initialize_v2(self.root / "config.json")
        active = resolve_current(self.root / "v3-staging")
        self.assertEqual("initialized", result["status"])
        self.assertEqual(result["version_id"], active["version_id"])
        self.assertTrue(active["database"].is_file())
        self.assertTrue((active["editions"] / "index.json").is_file())

    def test_provider_id_evidence_imports_legacy_alias(self):
        target=self.root/'catalogue.sqlite'
        result=self._build(target)
        self.assertEqual({'confirmed':1},result['legacy_aliases']['status_counts'])
        c=sqlite3.connect(target)
        try:
            row=c.execute("select status,v2_printing_id from legacy_aliases where legacy_id='legacy-fixture-print'").fetchone()
            self.assertEqual('confirmed',row[0])
            self.assertTrue(row[1].startswith('v2prn_'))
        finally: c.close()

    def test_phyrexian_source_keeps_raw_text_but_indexes_readable_name(self):
        record=dict(self.record,lang='ph',name='Vorinclex, Voice of Hunger',
                    printed_name='|lornScs',printed_type_line='|MewkfuDs',
                    printed_text='|gOcdrd')
        with gzip.open(self.archive,'wt',encoding='utf-8') as archive:
            archive.write(json.dumps(record)+'\n')
        target=self.root/'phyrexian.sqlite'
        self._build(target)
        with closing(sqlite3.connect(target)) as conn:
            row=conn.execute("SELECT name,printed_name,type_line,printed_type_line,printed_text,raw_json FROM source_records").fetchone()
            self.assertEqual(('Vorinclex, Voice of Hunger',None,'Creature',None,None),row[:5])
            self.assertEqual('|lornScs',json.loads(row[5])['printed_name'])
            self.assertEqual(('', '', ''),conn.execute("SELECT printed_name,printed_type_line,printed_text FROM source_search").fetchone())
        exported=json.loads((self.root/'exports'/'tst-fixture-set'/'index.json').read_text(encoding='utf-8'))['cards'][0]
        self.assertIsNone(exported['printed_name'])
        self.assertIsNone(exported['printed_type_line'])
        self.assertIsNone(exported['printed_text'])

    def test_interruption_before_replace_preserves_active_catalogue(self):
        target=self._old_target(); before=hashlib.sha256(target.read_bytes()).hexdigest()
        def interrupt(): raise RuntimeError('injected interruption before atomic replace')
        with self.assertRaisesRegex(RuntimeError,'injected interruption'):
            self._build(target,hook=interrupt)
        self.assertEqual(before,hashlib.sha256(target.read_bytes()).hexdigest())
        c=sqlite3.connect(target)
        try: self.assertEqual('previous',c.execute('select value from active').fetchone()[0])
        finally: c.close()
        self.assertFalse(list(self.root.glob('.v23-*.sqlite')))

    def test_truncated_archive_is_rejected_without_replacing_active_catalogue(self):
        target=self._old_target(); before=hashlib.sha256(target.read_bytes()).hexdigest()
        truncated=self.root/'truncated.jsonl.gz'
        truncated.write_bytes(self.archive.read_bytes()[:-8])
        with self.assertRaises((EOFError,OSError)):
            self._build(target,archive=truncated)
        self.assertEqual(before,hashlib.sha256(target.read_bytes()).hexdigest())
        c=sqlite3.connect(target)
        try: self.assertEqual('previous',c.execute('select value from active').fetchone()[0])
        finally: c.close()


if __name__=='__main__': unittest.main()
