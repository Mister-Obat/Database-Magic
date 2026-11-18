# Module — fonds historique et rapprochement

## Responsabilité

Inventorier les fichiers restants de `Cartes Magic/`, les anciens résultats de téléchargement
et les outils d’inventaire sans leur attribuer une impression V2 par supposition.
Il sert à expliquer, auditer ou reprendre le fonds initial, distinct du catalogue
actif.

## Emplacements

- `Magic/Prog/telecharger.py` et `Magic/Cartes.md` : ancien programme de cartes
  sélectionnées, avec sorties VF, anglaises et rapports.
- `_bibliotheque_mtg/src/python/bibliotheque_mtg/reconciliation.py` : inventaire et
  rapprochement de fichiers, alias d’édition, statut des candidats.
- `_bibliotheque_mtg/src/python/bibliotheque_mtg/historical_import.py` : reprise
  d'un plan JSON vérifié vers la version V2 active, sans déplacement de l'original.
- Dans ce même paquet, `collection.py`, `batch_collection.py`, `images.py`,
  `pipeline.py`, `publication.py` et `deployment.py` composent le pipeline
  antérieur de collecte/publication.
- `Magic/Prog/tests_bibliotheque/`,
  `_bibliotheque_mtg/src/python/tests_bibliotheque/` et `test_regles.py` conservent
  les tests historiques.
- Les inventaires et rapports de préservation sont sous
  `_bibliotheque_mtg/reports/` ; les originaux non attribués restent sous `Cartes Magic/`.

## Interfaces principales

- L’OCR de l’onglet Archives conserve chaque résultat réussi dans `_bibliotheque_mtg/state/archive-ocr/`
  par chemin d’origine. Le cache est invalidé si le fichier, la version active,
  l’algorithme OCR ou l’alias de dossier change. Le traitement en lot lance un
  nombre borné de lectures simultanées, enregistre chaque résultat terminé et
  cesse de lancer de nouvelles cartes à l’arrêt ; les lectures déjà lancées
  finissent. Si le titre, le code d’édition et le numéro imprimé prouvent une
  impression unique et que son slot est vide, l’image est importée et l’original
  retiré après contrôle du WebP organisé. Les slots déjà prêts, les doublons et
  les propositions incertaines restent à revoir manuellement. Le résultat OCR
  seul ne retire jamais une archive.
  Le cadrage du titre couvre aussi les anciennes cartes dont le titre est près
  du bord supérieur ; l'image complète est réservée aux cas où les régions
  ciblées ne suffisent pas.
  L'appariement du titre évalue le titre d'impression et le titre de chaque
  face physique (`card_faces`) : un scan de verso prouve la face correspondante
  et la cible porte l'index de cette face, au lieu du recto par défaut.
  Le texte du titre lu est exposé dans l'evidence de tous les statuts OCR
  (`ocr_title`, y compris sans proposition et ambigu) : il alimente la
  décision de nom d'affichage côté serveur, qui conserve un nom trouvable
  dans le catalogue et enregistre sinon un nom dérivé (titre de catalogue
  d'une proposition unique, nom de catalogue proche du titre lu, ou première
  ligne lisible du titre) dans l'état des archives résolues.
  Les noms des archives encore présentes survivent aux imports et suppressions
  d'autres fichiers ainsi qu'aux écritures simultanées du lot de nommage.
  Un fichier d'archive peut aussi être retiré sans import, sur action
  explicite de corbeille : le retrait est validé comme un import (racine,
  extension, taille, pas de lien symbolique, hors `Impossible/`) et consigné
  dans l'état des archives résolues avec le statut `discarded`.

- Une reprise ciblée des archives peut utiliser OCR local pour proposer une
  impression, mais la publication exige une identité unique prouvée par titre
  imprimé et numéro de collection, code d'édition lu sur l'image, ou dossier
  d'édition dont l'alias est `confirmed_exact_candidate`. Ce dernier cas est
  calibré sur les images déjà attribuées. Les dossiers « La lune hermétique »
  et « Obscure ascension » peuvent aussi être pris en compte après validation
  de leur titre français dans les galeries Wizards officielles et inspection
  d'images originales avec symbole/numéro d'édition. Les autres dossiers à
  alias ambigu restent exclus. Une suggestion imprécise reste
  dans l'archive. Chaque import vérifie le SHA-256 de l'original et réutilise
  la normalisation WebP 531×738 du catalogue actif.
- La reprise du fonds restant peut proposer une identité par OCR local ou
  comparaison avec un WebP déjà prêt. Une similarité globale seule ne prouve
  pas l'impression : contrôler aussi le titre, le symbole, le numéro, la langue
  et l'unicité du slot actif. Journaliser pour chaque original son SHA-256, ses
  preuves, sa cible et le résultat. Un original vérifié peut être supprimé après
  contrôle de l'image prête organisée ; une identité toujours incertaine après
  examen est déplacée dans `Cartes Magic/Impossible/` avec son ancien chemin
  conservé dans le journal.
  Pour les anciennes impressions sans numéro visible, un dossier d'édition
  confirmé, le titre imprimé et le nom de l'artiste lus sur la carte peuvent
  établir l'identité si leur combinaison désigne une seule impression dans le
  catalogue actif.
- Le dossier historique « Phyrexian (2023) » est une étiquette erronée pour un
  lot d'images New Phyrexia (`nph`) : un échantillon de douze cartes porte le
  même symbole d'édition et les titres concordent avec les impressions `nph`.
  La reprise de ce lot utilise le titre imprimé et l'unicité de l'impression
  dans `nph`, avec contrôle de langue, de face et du WebP publié ; les titres
  répétés ou OCR incertains restent à examiner.
- Le dossier « Kamigawa (2004) » contient en réalité un lot Neon Dynasty
  (`neo`) : un échantillon de douze cartes porte `NEO` et 2022. La même reprise
  par titre unique s'applique à ce lot. Les dossiers dont l'édition exacte est
  confirmée par les alias du catalogue peuvent également utiliser ce protocole
  conservateur ; les cartes à titre répété restent non attribuées.
- Pour une carte transformable, le titre imprimé peut désigner la face 1 ; la
  reprise vérifie alors le titre de cette face dans `raw_json.card_faces`, le
  code et le numéro imprimés, la langue et l'existence du slot de face avant
  publication. Aucun rabattement implicite vers la face 0.
- Le classement automatique dans `Impossible/` intervient après le balayage OCR
  complet des fichiers encore présents, la tentative d'import des propositions
  uniques et le contrôle de leurs SHA-256. Le journal distingue les propositions
  refusées des fichiers sans candidat. `Impossible/` sort des inventaires
  ordinaires pour éviter un traitement répété.
- L'état des archives résolues est distinct du rapport historique daté. La vue
  Archives filtre les chemins résolus à partir de cet état. Le 16 novembre 2026,
  4 024 originaux dont l'identité et l'image prête organisée ont été vérifiées
  ont été supprimés de `Cartes Magic/` sur demande explicite de l'utilisateur.
  Les rapports de préservation restent des traces historiques antérieures à ce nettoyage.
- L'état des chemins résolus survit à la publication d'une nouvelle version du
  catalogue : les anciens chemins restent masqués, tandis que chaque nouvelle
  cible est revalidée dans la base active avant import. Un identifiant de version
  ancien dans le journal ne doit pas bloquer la reprise des fichiers restants.
- Le dossier `Cartes Magic/Inconnu/` suit la même reprise prudente : une image est
  publiée uniquement après concordance unique du code d'édition, du numéro de
  collection et du titre lus localement. L'original est supprimé seulement après
  vérification du WebP 531×738 classé dans `Images/`. Le 16 novembre 2026, 453
  fichiers ont été traités selon cette règle ; 358 restent à identifier.
  Cette passe est documentée par `state/unknown-imported.json` mais son protocole
  d'OCR ponctuel n'est pas une commande applicative réutilisable.

- `audit --input ... --report ...` passe par `inventory_library` : catégories de
  fichier, hash, dimensions, doublons et alias d’édition.
- `reconcile --sample ... --dry-run` ou `--all-existing` compare les anciens
  assets aux métadonnées et écrit des preuves de concordance.
- `collect` et les commandes `publish`/`deploy` appartiennent à l’ancien parcours
  ; les deux `.bat` utilisateur V2 ne les déclenchent pas.
- Le téléchargeur `Magic/Prog` lit des noms ou URL de `Magic/Cartes.md` et peut
  ranger les cartes trouvées seulement en anglais dans `Magic/cartes anglaises/`.

## Invariants

- L’inventaire n’efface pas d’original et ne suit pas les liens symboliques des
  sous-dossiers.
- Une correspondance exige une preuve de série, langue, nom imprimé ou face
  pertinente ; les conflits gardent un statut non attribué, notamment `unresolved_identity`
  pour les fichiers et `ambiguous_candidates` pour les alias d’édition.
- Le dernier rapport historique avant nettoyage portait sur 5 053 entrées
  initialement sans identité certaine ; ses nombres sont datés et ne décrivent
  plus les fichiers présents après la reprise et le nettoyage.
- Les alias importés dans V2 ont un statut distinct (`confirmed`, `ambiguous`,
  `unmatched`) et ne doivent pas modifier une impression par simple similarité de
  nom.
- Les hashes historiques servent de preuve de préservation ; aucun ancien fichier
  ne doit être nettoyé pour rendre un rapport positif.

## Dépendances, risques et vérifications

- Dépend de la [base source et des identités](catalogue.manifest.md), des
  métadonnées [Wizards](../infra/wizards-gallery.manifest.md) et du
  [stockage local](../infra/stockage-local.manifest.md).
- Des dossiers anciens portent plusieurs noms pour une même édition ; vérifier les
  alias avant d’en déduire un doublon.
- Tester les images illisibles, les langues inconnues, les cartes à plusieurs
  faces et les correspondances multiples avant d’admettre un asset dans V2.
- Pour une reprise, lire d’abord le
  [workflow de réconciliation](../workflows/reconcilier-historique.md) et le
  [rapport de préservation](../../_bibliotheque_mtg/reports/v2-t4.5-preservation-pending-audit.json).
