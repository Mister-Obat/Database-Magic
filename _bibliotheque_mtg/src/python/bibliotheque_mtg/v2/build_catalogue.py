from __future__ import annotations
import gzip, hashlib, json, os, re, sqlite3, tempfile, time
from itertools import groupby
from pathlib import Path
from .catalogue import _create_schema, _faces, _identity_key, _json, _stable_id, _sha256, import_legacy_aliases
from . import SCHEMA_VERSION

LEGACY_FOLDER_MAP = {
    "1 Dominaria (2018)": "dom",
    "Core M12 (2012)": "m12",
    "Core M13 (2013)": "m13",
    "Core M19 (2019)": "m19",
    "Horizons du Modern 3 (2024)": "mh3",
    "Ixalan (2017)": "xln",
    "Kamigawa (2004)": "chk",
    "La lune hermétique (2016)": "emn",
    "LOTR (2023)": "ltr",
    "Magic Origins (2015)": "ori",
    "Obscure ascension (2012)": "avr",
    "Phyrexian (2023)": "one",
    "Theros (2013)": "ths",
    "Warhammer 40k (2022)": "40k",
}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_if_changed(path, content):
    path = Path(path)
    encoded = content.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return True


def _search_field(obj, key):
    values = [str(obj.get(key) or "")]
    values.extend(str(face.get(key) or "") for face in (obj.get("card_faces") or []) if isinstance(face, dict))
    return " ".join(value for value in values if value)


def build_catalogue(source_db, archive, target_db, *, archive_sha256, archive_as_of, edition_root, legacy_generation=None, pre_publish_hook=None, progress=None, total_records=None, source_name='Scryfall all-cards bulk'):
    """Offline, atomic build from archived Scryfall all-cards JSONL gzip."""
    source_db, archive, target_db, edition_root = map(Path, (source_db, archive, target_db, edition_root))
    if progress:
        progress(0.0, "Lecture de l'archive…")
    actual = _sha256(archive)
    if actual != archive_sha256:
        raise ValueError(f"archive SHA-256 mismatch: expected {archive_sha256}, got {actual}")
    target_db.parent.mkdir(parents=True, exist_ok=True)
    edition_root.mkdir(parents=True, exist_ok=True)
    started=time.perf_counter()
    source=sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
    source.row_factory=sqlite3.Row
    temp=None
    try:
        sets=[dict(r) for r in source.execute("SELECT id,code,name,set_type,released_at,digital,card_count,eligibility,exclusion_reason,raw_json FROM sets ORDER BY id")]
        eligible={s['id'] for s in sets if s['eligibility']=='eligible'}
        with tempfile.NamedTemporaryFile(prefix='.v23-',suffix='.sqlite',dir=target_db.parent,delete=False) as f: temp=f.name
        out=sqlite3.connect(temp)
        out.execute('PRAGMA journal_mode=DELETE'); out.execute('PRAGMA synchronous=FULL'); out.execute('PRAGMA foreign_keys=ON')
        _create_schema(out)
        out.executemany("INSERT INTO editions VALUES(?,?,?,?,?,?,?,?,?)", [(s['id'],s['code'],s['name'],s['set_type'],s['released_at'],s['digital'],s['card_count'],s['eligibility'],s['exclusion_reason']) for s in sets])
        out.execute("CREATE TEMP TABLE bulk (provider_id TEXT PRIMARY KEY,set_id TEXT,collector TEXT,oracle_id TEXT,layout TEXT,illustration TEXT,lang TEXT,face_count INTEGER,identity_key TEXT,raw_json TEXT NOT NULL)")
        parsed=0; lang_counts={}; seen=0
        total = max(1, int(total_records or 0))
        with gzip.open(archive,'rt',encoding='utf-8') as stream:
            batch=[]
            for line in stream:
                seen += 1
                raw=json.loads(line); sid=raw.get('set_id')
                if sid not in eligible: continue
                pid=str(raw.get('id') or '')
                if not pid: raise ValueError('eligible record without provider id')
                lang=str(raw.get('lang') or '').casefold()
                if not lang: raise ValueError(f'missing language: {pid}')
                faces=raw.get('card_faces') or []
                layout=str(raw.get('layout') or 'unknown')
                key=None
                if raw.get('collector_number') and (raw.get('oracle_id') or raw.get('illustration_id')):
                    key=_canonical([sid,str(raw['collector_number']),str(raw.get('oracle_id') or ''),layout,str(raw.get('illustration_id') or '')])
                batch.append((pid,sid,str(raw.get('collector_number') or ''),raw.get('oracle_id'),layout,raw.get('illustration_id'),lang,len(faces),key,_canonical(raw)))
                parsed+=1; lang_counts[lang]=lang_counts.get(lang,0)+1
                if len(batch)>=2500:
                    out.executemany('INSERT INTO bulk VALUES(?,?,?,?,?,?,?,?,?,?)',batch); batch.clear()
                if progress and total_records and seen % 5000 == 0:
                    progress(0.65 * min(seen / total, 1.0), f"{seen} / {total} lignes")
            if batch: out.executemany('INSERT INTO bulk VALUES(?,?,?,?,?,?,?,?,?,?)',batch)
        if progress:
            progress(0.65, f"{parsed} cartes retenues")
        # Group by exact identity key; rows without safe evidence remain isolated.
        out.execute('CREATE INDEX bulk_identity_idx ON bulk(identity_key,lang,provider_id)')
        oracle_counts=out.execute('SELECT oracle_id,COUNT(*) FROM bulk WHERE oracle_id IS NOT NULL GROUP BY oracle_id').fetchall()
        out.executemany('INSERT INTO oracle_cards VALUES(?,?)',oracle_counts)
        records_n=faces_n=exceptions_n=printing_n=0
        processed=0; last_report=0; parsed_total=max(1, parsed)
        cur=out.execute('SELECT provider_id,set_id,collector,oracle_id,layout,illustration,lang,face_count,identity_key,raw_json FROM bulk ORDER BY COALESCE(identity_key,"~"||provider_id),lang,provider_id')
        for identity, iterator in groupby(cur, key=lambda r:r[8]):
            rows=list(iterator)
            # keyed groups are confirmed only if every language appears once and face shape agrees
            langs=[r[6] for r in rows]
            if identity is None:
                subgroups=[([r], 'unresolved_identity','provider id retained; missing safe cross-language identity evidence','missing_set_number_or_identity_evidence') for r in rows]
            elif len(rows)==1:
                subgroups=[(rows,'single_language','unique source record with set, number and oracle or illustration evidence',None)]
            elif len(set(langs))==len(langs) and len({r[7] for r in rows})==1:
                subgroups=[(rows,'confirmed_multilang','same set id, collector number, oracle id, layout and illustration id',None)]
            elif len(set(langs))==len(langs):
                subgroups=[([r],'unresolved_identity','provider id retained; face structures differ','language_face_structure_mismatch') for r in rows]
            else:
                subgroups=[([r],'unresolved_identity','provider id retained; identity key collided within a language','ambiguous_identity_key') for r in rows]
            for sub,status,evidence,exception in subgroups:
                first=sub[0]; raw=json.loads(first[9]); key_tuple=(first[1],first[2],str(first[3] or ''),first[4],str(first[5] or '')) if identity else None
                printing_id=_stable_id(key_tuple,first[0] if identity is None or status=='unresolved_identity' else None)
                variant={k:raw[k] for k in ('variation','promo','frame_effects','border_color') if k in raw}
                out.execute('INSERT INTO printings VALUES(?,?,?,?,?,?,?,?,?)',(printing_id,first[1],first[2],first[3],first[4],first[5],status,evidence,_canonical(variant)))
                face_specs=_faces({'layout':first[4],'raw_json':first[9]})
                for face in face_specs:
                    out.execute('INSERT INTO card_faces VALUES(?,?,?)',(printing_id,face['face_index'],face['physical_image']))
                    out.execute('INSERT INTO image_slots VALUES(?,?,?,NULL,\'missing\',NULL,?)',(printing_id,face['face_index'],'fr',_canonical({'reason':'not_downloaded','source_archive_sha256':archive_sha256})))
                    faces_n+=1
                for r in sub:
                    obj=json.loads(r[9]); pid=r[0]; lang=r[6]
                    colors=obj.get('colors') or []; colorid=obj.get('color_identity') or []
                    artist=obj.get('artist_ids') or obj.get('artist') or []
                    mana=obj.get('cmc',obj.get('mana_value'))
                    readable_printed_name = None if lang == 'ph' else obj.get('printed_name')
                    readable_printed_type = None if lang == 'ph' else obj.get('printed_type_line')
                    readable_printed_text = None if lang == 'ph' else obj.get('printed_text')
                    vals=(pid,printing_id,lang,obj.get('uri'),obj.get('name') or '',readable_printed_name,obj.get('type_line'),readable_printed_type,obj.get('mana_cost'),mana,_canonical(colors),_canonical(colorid),''.join(sorted(colors)),''.join(sorted(colorid)),obj.get('rarity'),_canonical(artist),obj.get('oracle_text'),readable_printed_text,_canonical(obj))
                    out.execute('INSERT INTO source_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',vals)
                    out.execute('INSERT INTO printing_languages VALUES(?,?,?)',(printing_id,lang,pid))
                    if exception:
                        out.execute('INSERT INTO identity_exceptions VALUES(?,?,?,?)',(pid,exception,evidence,_canonical({'archive_sha256':archive_sha256,'as_of':archive_as_of,'set_id':first[1],'lang':lang}))); exceptions_n+=1
                    out.execute('INSERT INTO source_search VALUES(?,?,?,?,?,?,?,?,?)',(pid,str(obj.get('set') or ''),lang,_search_field(obj,'name'),readable_printed_name or '',_search_field(obj,'type_line'),readable_printed_type or '',_search_field(obj,'oracle_text'),readable_printed_text or ''))
                    records_n+=1
                printing_n+=1
            processed+=len(rows)
            if progress and processed-last_report>=5000:
                last_report=processed
                progress(0.65+0.15*min(processed/parsed_total,1.0), f"{processed} / {parsed_total} cartes traitées")
        if progress:
            progress(0.80, "Cartes groupées")
        # Canonical exports, deterministic order and safe relative paths.
        edition_docs=[]; exported=0; export_total=len(eligible)
        def slug(s):
            x=re.sub(r'[^a-z0-9]+','-',(str(s['code'])+'-'+str(s['name'])).casefold()).strip('-')
            return x or str(s['id'])
        for s in sets:
            if s['id'] not in eligible: continue
            exported+=1
            folder=slug(s)
            card_count=out.execute('SELECT COUNT(*) FROM source_records sr JOIN printings p USING(printing_id) WHERE p.set_id=?',(s['id'],)).fetchone()[0]
            dest=edition_root/folder; dest.mkdir(parents=True,exist_ok=True)
            json_path=dest/'index.json'; tmp_json=json_path.with_suffix('.json.tmp')
            legacy_folders=sorted(folder_name for folder_name,code in LEGACY_FOLDER_MAP.items() if code==s['code'])
            edition_data={k:s[k] for k in ('id','code','name','set_type','released_at','digital','card_count')}
            if legacy_folders: edition_data['legacy_folders']=legacy_folders
            header={'schema_version':'2.0.0','edition':edition_data,'source':{'name':source_name,'as_of':archive_as_of,'sha256':archive_sha256},'card_records':card_count,'cards':[]}
            with tmp_json.open('w',encoding='utf-8',newline='\n') as handle:
                handle.write('{"schema_version":"2.0.0","edition":'+_canonical(header['edition'])+',"source":'+_canonical(header['source'])+',"card_records":'+str(card_count)+',"cards":[')
                card_cur=out.execute('SELECT sr.provider_card_id,sr.lang,sr.raw_json,p.printing_id,p.collector_number,p.layout FROM source_records sr JOIN printings p USING(printing_id) WHERE p.set_id=? ORDER BY p.collector_number,sr.lang,sr.provider_card_id',(s['id'],))
                first_card=True
                for pid,lang,raw_json,printing_id,number,layout in card_cur:
                    obj=json.loads(raw_json); colors=obj.get('colors') or []; colorid=obj.get('color_identity') or []
                    card={'printing_id':printing_id,'provider_card_id':pid,'language':lang,'name':obj.get('name'),'printed_name':None if lang == 'ph' else obj.get('printed_name'),'type_line':obj.get('type_line'),'printed_type_line':None if lang == 'ph' else obj.get('printed_type_line'),'mana_cost':obj.get('mana_cost'),'mana_value':obj.get('cmc',obj.get('mana_value')),'colors':colors,'color_identity':colorid,'rarity':obj.get('rarity'),'collector_number':number,'layout':layout,'oracle_text':obj.get('oracle_text'),'printed_text':None if lang == 'ph' else obj.get('printed_text'),'faces':obj.get('card_faces') or [],'image_status':'missing'}
                    if not first_card: handle.write(',')
                    handle.write(_canonical(card)); first_card=False
                handle.write(']}\n')
            _write_if_changed(json_path,tmp_json.read_text(encoding='utf-8')); tmp_json.unlink(missing_ok=True)
            md_path=dest/'index.md'; md_tmp=dest/'index.md.tmp'
            with md_tmp.open('w',encoding='utf-8',newline='\n') as handle:
                handle.write(f"# {s['name']}\n\nCode : `{s['code']}`\n\nEnregistrements source : {card_count}\n\n| N° | Langue | Nom | Type | Coût | Rareté | Image |\n|---|---|---|---|---|---|---|\n")
                card_cur=out.execute('SELECT sr.lang,sr.name,sr.printed_name,sr.type_line,sr.printed_type_line,sr.mana_cost,sr.rarity,p.collector_number FROM source_records sr JOIN printings p USING(printing_id) WHERE p.set_id=? ORDER BY p.collector_number,sr.lang,sr.provider_card_id',(s['id'],))
                for lang,name,printed_name,type_line,printed_type,mana,rarity,number in card_cur:
                    vals=[number,lang,printed_name or name or '',printed_type or type_line or '',mana or '',rarity or '','missing']
                    handle.write('| '+' | '.join(str(x).replace('|','\\|').replace('\n',' ') for x in vals)+' |\n')
            _write_if_changed(md_path,md_tmp.read_text(encoding='utf-8')); md_tmp.unlink(missing_ok=True)
            image_path=dest/'images.json'; image_tmp=dest/'images.json.tmp'
            with image_tmp.open('w',encoding='utf-8',newline='\n') as handle:
                slots_count=out.execute('SELECT COUNT(*) FROM printings p JOIN card_faces f USING(printing_id) WHERE p.set_id=?',(s['id'],)).fetchone()[0]
                handle.write('{"schema_version":"2.0.0","edition_id":'+_canonical(s['id'])+',"slots":[')
                slot_cur=out.execute('SELECT p.printing_id,f.face_index FROM printings p JOIN card_faces f USING(printing_id) WHERE p.set_id=? ORDER BY p.collector_number,p.printing_id,f.face_index',(s['id'],))
                first_slot=True
                for printing_id,face_index in slot_cur:
                    slot={'printing_id':printing_id,'face_index':face_index,'preferred_language':'fr','selected_language':None,'status':'missing','path':None}
                    if not first_slot: handle.write(',')
                    handle.write(_canonical(slot)); first_slot=False
                handle.write(']}\n')
            _write_if_changed(image_path,image_tmp.read_text(encoding='utf-8')); image_tmp.unlink(missing_ok=True)
            edition_docs.append({'id':s['id'],'code':s['code'],'name':s['name'],'folder':folder,'index_json':f'{folder}/index.json','index_md':f'{folder}/index.md','card_records':card_count,'image_slots':slots_count,'legacy_folders':legacy_folders})
            if progress and (exported%10==0 or exported==export_total):
                progress(0.80+0.18*exported/max(1,export_total), f"{exported} / {export_total} éditions exportées")
        rootdoc={'schema_version':'2.0.0','source':{'name':source_name,'as_of':archive_as_of,'sha256':archive_sha256},'edition_count':len(edition_docs),'editions':edition_docs}
        _write_if_changed(edition_root/'index.json',_canonical(rootdoc)+'\n')
        _write_if_changed(edition_root/'index.md','# Catalogue Magic\n\n'+''.join(f"- [{e['name']} ({e['code']})]({e['index_md']}) — {e['card_records']} enregistrements\n" for e in edition_docs))
        out.execute("INSERT INTO printing_added SELECT printing_id,strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM printings")
        meta={'schema_version':SCHEMA_VERSION,'source_database_sha256':_sha256(source_db),'source_archive_sha256':archive_sha256,'source_as_of':archive_as_of,'eligible_editions':str(len(eligible)),'deferred_editions':str(len(sets)-len(eligible)),'source_records':str(records_n),'printings':str(printing_n),'identity_exceptions':str(exceptions_n),'faces':str(faces_n)}
        out.executemany('INSERT INTO catalogue_meta VALUES(?,?)',sorted(meta.items())); out.commit()
        alias_summary={'legacy_ids':0,'status_counts':{}}
        if legacy_generation is not None:
            out.close()
            alias_summary=import_legacy_aliases(temp,legacy_generation)
            out=sqlite3.connect(temp)
            out.execute("INSERT OR REPLACE INTO catalogue_meta VALUES('legacy_aliases',?)",(str(alias_summary['legacy_ids']),))
            out.commit()
        integrity=out.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity!='ok': raise sqlite3.DatabaseError(integrity)
        slots=out.execute('SELECT COUNT(*) FROM image_slots').fetchone()[0]
        if pre_publish_hook is not None:
            pre_publish_hook()
        out.close(); os.replace(temp,target_db); temp=None
        if progress:
            progress(1.0, "Catalogue construit")
        return {'status':'valid','integrity':integrity,'eligible_editions':len(eligible),'deferred_editions':len(sets)-len(eligible),'source_records':records_n,'language_counts':lang_counts,'printings':printing_n,'identity_exceptions':exceptions_n,'faces':faces_n,'slots':slots,'legacy_aliases':alias_summary,'database_bytes':target_db.stat().st_size,'elapsed_seconds':round(time.perf_counter()-started,3),'target_sha256':_sha256(target_db),'edition_root':str(edition_root)}
    finally:
        if 'out' in locals():
            try: out.close()
            except sqlite3.Error: pass
        source.close()
        if temp and os.path.exists(temp): os.unlink(temp)
