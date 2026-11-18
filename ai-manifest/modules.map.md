# Modules → code et données

Les chemins sont relatifs à la racine du projet. Cette carte distingue l’arbre V2
actif de l’arbre historique. Les liens vers les manifests expliquent les contrats
; ce fichier sert à trouver les emplacements.

| Module | Code principal | Contrats / tests associés |
|---|---|---|
| [Catalogue](modules/catalogue.manifest.md) | [`sources.py`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/sources.py), [`v2/catalogue.py`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/v2/catalogue.py), [`v2/build_catalogue.py`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/v2/build_catalogue.py) | [`v2/schema.sql`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/v2/schema.sql), `tests_v2/test_catalogue_v2.py`, `tests_v2/test_builder_atomic_v23.py` |
| [Interface et recherche](modules/interface-recherche.manifest.md) | [`local_app.py`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/local_app.py), [`v2/api.py`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/v2/api.py), [`v2/query.py`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/v2/query.py), [`ui/`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/ui/) | `tests_v2/test_local_app.py`, `tests_v2/test_query_v2.py` |
| [Images](modules/images.manifest.md) | [`image_download.py`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/image_download.py), [`organized_images.py`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/organized_images.py) | `tests_v2/test_download_v2.py`, `state/v2-image-download-ledger.json`, `versions/<id>/assets/webp/`, `_bibliotheque_mtg/Images/` |
| [Maintenance et publication](modules/maintenance-publication.manifest.md) | [`maintenance.py`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/maintenance.py), [`v2/publish.py`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/v2/publish.py), [`__main__.py`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/__main__.py) | `tests_v2/test_maintenance_v2.py`, `tests_v2/test_publish_v23.py`, `v3-staging/current.json` |
| [Decks préconstruits](modules/decks-preconstruits.manifest.md) | [`decks.py`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/decks.py) | `tests_v2/test_decks.py`, base SQLite locale dédiée |
| [Génération d’illustration](modules/card-generation.manifest.md) | [`v2/card_generation.py`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/v2/card_generation.py) | candidates PNG privées sous le dossier d’état fourni par l’appelant |
| [Historique](modules/historique.manifest.md) | [`reconciliation.py`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/reconciliation.py), [`historical_import.py`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/historical_import.py), [`pipeline.py`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/pipeline.py), [`collection.py`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/collection.py), [`batch_collection.py`](../_bibliotheque_mtg/src/python/bibliotheque_mtg/batch_collection.py) | [`Magic/Prog`](../Magic/Prog/), [`Magic/Cartes.md`](../Magic/Cartes.md), `tests_bibliotheque/`, `test_regles.py`, `state/historical-archive-resolved.json`, `state/unknown-imported.json` |

## Points d’entrée et stockage

- Lanceurs utilisateur :
  [`Lancer_Bibliotheque_Magic.bat`](../Lancer_Bibliotheque_Magic.bat),
  [`Mettre_a_jour_bibliotheque.bat`](../Mettre_a_jour_bibliotheque.bat).
- Configuration :
  [`_bibliotheque_mtg/config.json`](../_bibliotheque_mtg/config.json) ; schémas
  JSON de l’ancien pipeline dans
  [`_bibliotheque_mtg/schemas/`](../_bibliotheque_mtg/schemas/).
- Sources enregistrées : `_bibliotheque_mtg/sources/scryfall/` et
  `_bibliotheque_mtg/sources/wizards/` ; base source
  `_bibliotheque_mtg/catalogue.sqlite`.
- Catalogue consulté : `_bibliotheque_mtg/v3-staging/current.json` puis
  `versions/<id>/catalogue.sqlite`, `editions/` et `assets/`.
- Images prêtes organisées : `_bibliotheque_mtg/Images/` ; les fichiers
  historiques restant à identifier sont dans [`Cartes Magic/`](../Cartes%20Magic/).
  Les copies de sauvegarde se trouvent dans `_bibliotheque_mtg/backups/`.

Les rapports sous `_bibliotheque_mtg/reports/` sont des preuves historiques ou des
sorties de traitement. Le
[rapport de validation](../_bibliotheque_mtg/reports/validation_v2.md) donne les
chiffres de la dernière V2 ; les manifests restent centrés sur les contrats du
code.
