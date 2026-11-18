# Infrastructure — stockage local et versions

## Responsabilité

Conserver séparément les originaux, les archives de fournisseurs, la base source,
les versions V2, les états de téléchargement et les sauvegardes. La recherche lit
seulement la version désignée par `current.json`.

## Emplacements

| Racine | Contenu et propriétaire logique |
|---|---|
| `Cartes Magic/` | Fichiers historiques non attribués et métadonnées ; les images déjà indexées et vérifiées ont été nettoyées sur demande explicite. |
| `_bibliotheque_mtg/sources/` | Réponses et archive Scryfall ; pages et catalogue Wizards. |
| `_bibliotheque_mtg/catalogue.sqlite` | Base source créée par `sources.py`, distincte du catalogue V2 servi. |
| `_bibliotheque_mtg/v3-staging/current.json` | Pointeur de contrat `2.0.0` avec chemins relatifs `database` et `editions`. |
| `_bibliotheque_mtg/v3-staging/versions/<id>/` | SQLite V2, exports d’édition et WebP associés. |
| `_bibliotheque_mtg/Images/` | Collection permanente navigable des images prêtes, un sous-dossier par édition et un fichier par impression/face. Les fichiers de la version active et ceux de cette collection sont liés physiquement sur le même volume quand possible. |
| `_bibliotheque_mtg/state/` | Ledger durable des essais d’image. |
| `_bibliotheque_mtg/reports/` | Journaux, audits, métriques et validations historiques. |
| `_bibliotheque_mtg/backups/` | Sauvegardes et restaurations de contrôle. |

Pour une installation existante, la fonction partagée de chemin utilise encore
le pointeur hérité sous `_bibliotheque_mtg/work/v3-staging/current.json` lorsque
le pointeur du nouvel emplacement est absent. Les candidates IA sont écrites
dans `state/ai-card-candidates/` sur les nouvelles installations, avec repli
sur l'ancien dossier `work/ai-card-candidates/` tant qu'il existe.

## Publication et lecture

- `v2/publish.atomic_publish_pointer` rejette les chemins absolus, sortant de la
  racine ou inexistants ; il écrit un JSON temporaire avant `os.replace`.
- `v2/publish.resolve_current` valide la version du contrat et la présence des
  chemins pointés. L’application le rappelle à chaque requête de catalogue.
- `v2/query.connect` ouvre la base en mode SQLite `ro` pour la consultation ; le
  module d’image ouvre une transaction pour changer un slot.
- La version active utilisée pour les téléchargements adopte le journal SQLite
  WAL sur disque local : les lectures de l'interface ne bloquent pas la
  validation des nouvelles images. Le mode est établi avant les lectures du
  serveur et vérifié au début d'un téléchargement.
- Les exports de chaque édition sont rangés sous un dossier dérivé de code et nom
  ; `editions/index.json` donne le dossier exact à utiliser.
- Les fichiers WebP portent un hash de contenu dans leur chemin et leur provenance
  en base.
- Un miroir organisé des slots prêts est maintenu sous `Images/`, en conservant
  les chemins internes par hash de la base pour les API et les mises à jour.
  Les nouveaux téléchargements et imports synchronisent ce miroir après publication.
  Au démarrage, un fichier interne absent peut être recréé depuis son image
  organisée après contrôle de son hash ; `Images/` garde donc le fonds durable.

## Opérations et invariants

1. Une mise à jour construit une version temporaire, vérifie sa base et ne bascule
   le pointeur qu’après installation du dossier final.
2. Une image à la demande enrichit la version active en place ; le pointeur ne
   change pas pour ce seul ajout.
3. `backup_current` crée un dossier nouveau avec SQLite et les exports, sans
   `assets/`, et un `backup-manifest.json` ; `restore_backup` valide les fichiers
   non image et copie sous une autre racine. `Images/` est préservé séparément.
4. Le restore ne rend pas automatiquement sa copie active. La consultation
   continue à résoudre le pointeur actuel.
5. La publication V2 ne supprime pas automatiquement les originaux historiques
   ni les archives fournisseur. Un nettoyage explicite ne cible que des images
   déjà attribuées, avec SHA-256 source et image prête organisée vérifiés.

## Risques et points de contrôle

- Le dernier catalogue V2 validé contient une base SQLite de plusieurs Go ;
  vérifier l’espace disponible avant mise à jour ou sauvegarde.
- La copie SQLite utilise l'API de sauvegarde en ligne ; éviter les changements
  d'exports pendant la sauvegarde. Vérifier `PRAGMA integrity_check` et les hashes
  de la restauration, puis la disponibilité de `Images/` avant réactivation.
- La racine `v3-staging` est un nom de dossier hérité ; le contrat actuel du
  pointeur reste `2.0.0`.
- Voir [architecture](../architecture.md),
  [maintenance](../modules/maintenance-publication.manifest.md),
  [restauration](../workflows/sauvegarde-restauration.md) et
  [risques](../dependencies.risks.md).

## Decks personnels

Les compositions personnelles sont indépendantes des versions V2 et de l'index
préconstruit. `state/personal-decks.sqlite` conserve les decks et chaque révision
validée, dans une transaction SQLite avec `synchronous=FULL`. Les sauvegardes
refusent les révisions périmées. Une copie SQLite cohérente est remplacée
atomiquement sous `state/personal-decks-backups/latest.sqlite` après modification.
Une erreur de copie secondaire est journalisée, sans annoncer l'échec d'une
transaction principale déjà validée. Les brouillons non confirmés sont conservés
dans le navigateur ; seule la réponse serveur autorise le statut Enregistré.
Ces fichiers ne sont ni remplacés ni nettoyés par une publication de catalogue.
Préserver aussi cette base et ses sauvegardes lors d'une copie de la bibliothèque.
