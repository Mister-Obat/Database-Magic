from __future__ import annotations
import json,os,tempfile
from pathlib import Path,PurePosixPath

CONTRACT_VERSION='2.0.0'


def atomic_publish_pointer(root, version_id, database_path, editions_path, *, pre_replace_hook=None):
    root=Path(root).resolve()
    def safe_existing(value):
        rel=PurePosixPath(str(value))
        if rel.is_absolute() or '..' in rel.parts:
            raise ValueError(f'unsafe publication path: {value}')
        resolved=(root/Path(*rel.parts)).resolve()
        if not resolved.is_relative_to(root) or not resolved.exists():
            raise ValueError(f'publication target missing or outside root: {value}')
        return rel.as_posix()
    db_rel=safe_existing(database_path); editions_rel=safe_existing(editions_path)
    pointer=root/'current.json'
    document={'contract_version':CONTRACT_VERSION,'version_id':str(version_id),'database':db_rel,'editions':editions_rel}
    payload=json.dumps(document,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n'
    fd,tmp=tempfile.mkstemp(prefix='.current-',suffix='.json',dir=root)
    os.close(fd)
    try:
        Path(tmp).write_text(payload,encoding='utf-8')
        if pre_replace_hook is not None: pre_replace_hook()
        os.replace(tmp,pointer)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    return document


def resolve_current(root):
    root=Path(root).resolve(); doc=json.loads((root/'current.json').read_text(encoding='utf-8'))
    if doc.get('contract_version')!=CONTRACT_VERSION: raise ValueError('unsupported publication contract')
    resolved={key:(root/doc[key]).resolve() for key in ('database','editions')}
    if any(not p.is_relative_to(root) or not p.exists() for p in resolved.values()): raise ValueError('invalid current pointer')
    return {'version_id':doc['version_id'],**resolved}
