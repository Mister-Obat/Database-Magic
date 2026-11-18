# Workflow — inventorier et réconcilier le fonds historique

## Déclencheur et résultat

Ce flux sert lorsqu’un ancien dossier, PNG ou résultat de lot doit être relié au
nouveau catalogue. Le résultat attendu est un rapport de preuves. Le rapprochement emploie notamment
`matched` et `unresolved_identity` ; les alias importés dans V2 ont leurs propres
statuts `confirmed`, `ambiguous` et `unmatched`. Le rapprochement seul ne
supprime aucun original de `Cartes Magic/` ; un nettoyage ultérieur explicite
exige une image prête et contrôlée dans le catalogue organisé.

## Préconditions

- Fonds historique et base source `_bibliotheque_mtg/catalogue.sqlite`
  accessibles.
- Galerie Wizards archivée si les indices de nom de fichier doivent être comparés.
- Distinguer les fichiers anciens des assets de `v3-staging/versions/<id>/`.

## Étapes

1. `inventory_library` parcourt `Cartes Magic/` sans suivre les liens symboliques,
   relève chemins, tailles, SHA-256, dimensions, catégories et doublons. Le
   sous-dossier `Impossible/` contient des fichiers déjà examinés et sort de
   l'inventaire des cartes encore à reprendre.
2. L’inventaire écrit un rapport JSON et des alias d’édition dans
   `_bibliotheque_mtg/reports/`. Les correspondances de nom d’asset Wizards sont
   consignées comme indices.
3. `reconcile_sample` ou `reconcile_all_existing` compare les fichiers inventoriés
   aux séries, langues, noms imprimés et faces de la base source, puis écrit les
   candidats et le statut.
4. Contrôler chaque fichier `matched` avant import vers V2 ; conserver les
   rapprochements non résolus et les alias `ambiguous` ou `unmatched` dans
   l’archive sans association fictive.
5. Pour une importation locale approuvée, contrôler le SHA-256 de l'original,
   l'identité exacte de l'impression et le WebP 531×738 publié. Le dossier
   `Inconnu/` requiert simultanément code d'édition, numéro de collection et titre
   lus localement ; son original ne peut être supprimé qu'après ce contrôle.
   Cette phase d'import et de nettoyage explicite est distincte du rapprochement
   en lecture seule des étapes précédentes.

## Commandes et fichiers

- Le CLI actif dans `_bibliotheque_mtg/src/python` expose
  `audit --input ... --report ...` et
  `reconcile --all-existing --dry-run --report ...`.
  La variante `--all-existing` recrée son inventaire courant avant de
  réconcilier ; un ancien `inventory-production.json` ne doit pas masquer les
  fichiers ajoutés depuis le dernier audit.
- Pour appliquer un plan historique déjà vérifié, depuis ce même dossier :

  ```powershell
  python -m bibliotheque_mtg.historical_import chemin\vers\plan.json --apply
  ```

  Sans `--apply`, la commande contrôle le plan sans publier ni modifier les
  fichiers. Le plan est produit séparément et doit contenir les preuves d'identité
  exigées par `historical_import.py`.
- La [carte du module historique](../modules/historique.manifest.md) pointe vers
  `reconciliation.py`, les anciens collecteurs et les rapports.
- Les deux lanceurs utilisateur V2 ne déclenchent pas `collect`, `publish` ou
  `deploy` de l’ancien pipeline.
- Le rapport
  `_bibliotheque_mtg/reports/v2-t4.1-historical-unidentified-index.json` alimente
  l’onglet « archive » de l’application.

## Invariants et cas d’échec

- Le nom d’une carte ou d’un fichier ne suffit pas pour identifier une impression
  exacte ; série, langue, face et preuve de source sont nécessaires.
- Un PNG illisible, une langue inconnue ou plusieurs candidats restent classés
  pour examen ; ils ne deviennent pas une image `ready` par défaut.
- Si la base source, l’inventaire ou les alias manquent, stopper le rapprochement
  et restaurer ces prérequis avant de conclure.
- Les anciens rapports de préservation décrivent l'état avant les imports et les
  nettoyages demandés. Ils restent des preuves historiques mais ne comptent plus
  les fichiers présents ; consulter l'inventaire courant avant toute reprise.
