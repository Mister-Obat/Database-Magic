import json,tempfile,unittest
from pathlib import Path
from bibliotheque_mtg.v2.publish import atomic_publish_pointer,resolve_current

class PublishPointerTests(unittest.TestCase):
 def test_pointer_replacement_is_atomic_and_resolves_only_inside_root(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d); old=root/'versions'/'old'; new=root/'versions'/'new'
   for folder in (old,new):
    (folder/'editions').mkdir(parents=True); (folder/'catalogue.sqlite').write_bytes(b'db'); (folder/'editions'/'index.json').write_text('{}',encoding='utf-8')
   atomic_publish_pointer(root,'old','versions/old/catalogue.sqlite','versions/old/editions')
   before=(root/'current.json').read_bytes()
   def fail(): raise RuntimeError('interrupted before pointer replace')
   with self.assertRaisesRegex(RuntimeError,'interrupted'):
    atomic_publish_pointer(root,'new','versions/new/catalogue.sqlite','versions/new/editions',pre_replace_hook=fail)
   self.assertEqual(before,(root/'current.json').read_bytes())
   self.assertEqual('old',resolve_current(root)['version_id'])
   atomic_publish_pointer(root,'new','versions/new/catalogue.sqlite','versions/new/editions')
   self.assertEqual('new',resolve_current(root)['version_id'])
 def test_rejects_parent_traversal(self):
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaises(ValueError): atomic_publish_pointer(d,'x','../outside.sqlite','.')

if __name__=='__main__': unittest.main()
