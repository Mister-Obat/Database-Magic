# Module — catalogue et identités

## Responsabilité

Importer les métadonnées des fournisseurs, décider quelles éditions sont
éligibles, conserver chaque ligne source et construire les impressions,
traductions, faces et exports du catalogue V2. C’est la source de vérité textuelle
de l’application locale.

## Édition locale Proxy

Le dossier `proxy/` à la racine contient les images personnelles. Le module
`proxies.py` produit un index SQLite dans `_bibliotheque_mtg/state/` et
enregistre séparément les associations fichier → impression réelle. Le nom du
fichier identifie le proxy. Une association reprend les caractéristiques et
traductions de l’impression source dans l’index local, avec des identifiants
propres, le code d’édition `proxy` et l’image du dossier. La base publiée reste
en lecture seule pour ce parcours. La suppression du fichier retire le proxy
du catalogue.
Le cache est reconstruit lorsque le fichier des associations ou l’index
dérivé change sur disque. Les associations restent la source de vérité.

Le remplacement depuis la fiche réencode l’image dans le format de son fichier
local et conserve son identifiant et son association. Les decks personnels
stockent l’impression Proxy et chargent son image et ses données courantes.

## Emplacements

- `sources.py` : client HTTP, cache, validation gzip JSONL, pages Scryfall et
  galeries Wizards, base source `_bibliotheque_mtg/catalogue.sqlite`.
- `v2/build_catalogue.py` : reconstruction complète depuis All Cards et les
  séries, création de la base V2 et des exports d’édition.
- `v2/catalogue.py` : règles d’identité, faces physiques, migration V2 et alias
  historiques ; `v2/schema.sql` définit les tables et FTS5.
- `tests_v2/test_catalogue_v2.py`, `tests_v2/test_builder_atomic_v23.py` et
  `tests_v2/test_query_v2.py` couvrent identité, construction et lecture.
- `sources.py` écrit `reports/french-text-coverage.json` à chaque actualisation,
  y compris lorsqu'aucun nombre d'édition ne change. Le rapport distingue les
  lignes Scryfall françaises des champs imprimés effectivement présents; le
  compteur de complétude exige nom, type et texte FR lorsque la carte a du texte
  de règles anglais. Une lacune reste explicitement `incomplete`.
- `bibliotheque_mtg/data/official_french_text_overrides.json` contient les
  traductions officielles françaises vérifiées qui manquent aux archives
  automatiques. Elles sont importées de manière idempotente dans la table source
  `official_french_text`, séparée des IDs et enregistrements Scryfall. Chaque
  entrée exige un ID fournisseur exact, un Oracle, un code et numéro, des champs
  FR, une URL de preuve, une image officielle et une date de revue.

La [carte des fichiers](../modules.map.md) donne les liens directs. La version
active se résout par `v3-staging/current.json`.

## Entrées, sorties et interfaces

`printing_added` conserve la première date d'apparition locale de chaque
`printing_id` dans les nouveaux catalogues. La maintenance reprend cette date
par identité exacte entre publications ; les impressions absentes de la
version précédente reçoivent la date de construction.

`v2.query.card_legalities` restitue les statuts Scryfall par format pour les
impressions demandées. Les catalogues historiques possèdent déjà ces données
dans `source_records.raw_json`. Le format absent ou le statut fournisseur
inconnu reste sans statut exploitable.
La recherche ciblée d’Info conserve les interdictions et restrictions par
Oracle dans `card-restrictions.json` à la racine de publication. Le relevé
remplace les statuts `banned` et `restricted` pour les formats couverts, y
compris lorsqu’une carte sort de la liste. Un statut retiré n’est pas assimilé
à `legal`. Le relevé est compatible avec les versions partageant le hash de
l’archive complète qui lui sert de base.

1. `run_sources(config_path, refresh=True, metadata_only=True)` relève le
   descripteur All Cards, l’archive, les séries et les galeries ; il écrit les
   sources locales et `reports/sources.json`.
   Pour le bouton Info, le mode `additions_only` compare les IDs et nombres de
   cartes des séries à la version publiée, récupère uniquement les séries ayant
   des ajouts, puis construit un catalogue limité à ces séries avant fusion dans
   une copie validée de la version publiée.
2. `build_catalogue(...)` vérifie le SHA-256 de l’archive, filtre les séries
   éligibles et construit SQLite avec un enregistrement par ID fournisseur.
3. Les groupes d’impression sont confirmés seulement avec série, numéro de
   collection, oracle ou illustration, layout et forme des faces. Une collision
   reste `unresolved_identity`.
4. Chaque série éligible reçoit `index.json`, `index.md` et `images.json`. La
   racine `editions/` reçoit aussi ses deux index.
5. `v2/query.py` lit ensuite la base créée ; ce module ne décide pas de
   l’affichage de la fiche ni du téléchargement d’image.
6. Le nom fournisseur de l'illustrateur (`artist`) est indexé dans la recherche
   des nouvelles publications. Le catalogue actif hérité dispose d'un index
   FTS `artist_search` local construit depuis `raw_json`, sans téléchargement.
   Lors d’une fusion depuis une version à neuf colonnes, l’index FTS de sa
   copie de travail adopte le schéma du delta. Les éditions inchangées
   conservent leurs champs indexés et retrouvent les illustrateurs depuis
   `raw_json`. L’index séparé devient alors redondant.

## Invariants

- L’ID Scryfall d’une ligne éligible doit apparaître une fois dans
  `source_records` ou dans une exception documentée ; l’export et SQLite doivent
  représenter le même ensemble.
- Une paire FR/EN n’est fusionnée que lorsque l’identité est démontrée. Les lignes
  sans preuve restent séparées et traçables par `provider_card_id`.
- Pour une ligne `lang='ph'`, `name` conserve le nom de carte lisible. Le
  texte imprimé transcrit en glyphes phyrexians reste dans `raw_json`, mais les
  champs de consultation `printed_name`, `printed_type_line`, `printed_text` et
  leurs index de recherche sont vides. Cette normalisation est appliquée à la construction et au
  catalogue publié sans changer l'identité ni la provenance Scryfall.
- `card_faces` suit la disposition physique : les layouts `transform`,
  `modal_dfc`, `double_faced_token` et `reversible_card` peuvent avoir plusieurs
  images ; d’autres cartes à textes multiples ne créent qu’un slot.
- Le nombre de slots `missing` est créé lors du build sans requête d’image.
  `ready` ne doit provenir que d’un asset vérifié.
- Le build n’expose pas sa base cible avant `PRAGMA integrity_check=ok` et le
  remplacement atomique du fichier temporaire.

## Dépendances et risques

- [Scryfall](../infra/scryfall.manifest.md) pour le texte ;
  [Wizards](../infra/wizards-gallery.manifest.md) pour le relevé de galeries ;
  [stockage local](../infra/stockage-local.manifest.md) pour les archives et
  versions.
- [Maintenance](maintenance-publication.manifest.md) décide quand reconstruire et
  publier ; [interface](interface-recherche.manifest.md) consomme les tables ;
  [historique](historique.manifest.md) fournit les alias prouvés.
- L’import est volumineux. Une archive tronquée, un changement de schéma
  fournisseur ou un manque d’espace doit échouer avant changement de
  `current.json`.

## Points à vérifier lors d’une intervention

- Comparer les IDs des éditions éligibles entre archive, SQLite et exports ;
  vérifier zéro omission ou doublon.
- Relire `identity_exceptions` avant d’améliorer une association de langues ou de
  variantes.
- Tester les layouts recto/verso et les cartes à plusieurs textes pour préserver
  le nombre de faces physiques.
- Vérifier le rapport de source, les empreintes et les éditions différées du
  snapshot actif avant d’interpréter un compteur de couverture.
- Ne jamais présenter la couverture Scryfall ou la couverture combinée comme
  complète si le rapport indique des Oracle sans champs officiels FR. Le fichier
  MTGJSON par édition peut être consulté pour un gap ciblé; `40K.json.bz2` ne
  contient pas de `foreignData` français pour 40K/176.
