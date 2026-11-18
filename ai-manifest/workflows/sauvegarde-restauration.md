# Workflow — sauvegarder et restaurer une version V2

## Déclencheur et résultat

Une sauvegarde est utile avant un import risqué, un changement de stockage ou une
réparation. `backup` produit une copie du catalogue actif et de ses exports,
sans les WebP de `assets/`, avec tailles et SHA-256 ; `restore` vérifie cette
copie et la place dans une racine isolée, sans changer l’application en cours.
La collection durable `Images/` doit être préservée séparément.

## Préconditions

- Aucune image en cours de téléchargement : `backup_current` copie des fichiers
  actifs sans verrou commun avec `image_download.py`.
- Une destination de sauvegarde encore absente et suffisamment d’espace libre.
- Lancer le module Python depuis `_bibliotheque_mtg/src/python`, qui contient le
  paquet `bibliotheque_mtg`.

## Commandes

```powershell
$projet = (Get-Location).Path
$ops = "$projet\_bibliotheque_mtg"
$python = "$ops\.venv\Scripts\python.exe"
$config = "$ops\config.json"
Set-Location "$ops\src\python"
$copie = "$ops\backups\v2-$(Get-Date -Format yyyyMMdd-HHmmss)"
& $python -m bibliotheque_mtg --config $config backup --destination $copie
$racineRestauree = "$ops\backups\restore-$(Get-Date -Format yyyyMMdd-HHmmss)"
& $python -m bibliotheque_mtg --config $config restore --backup $copie --destination-root $racineRestauree
```

La syntaxe CLI `backup`/`restore` est vérifiée dans `__main__.py` ; la validation
V2 a exécuté une sauvegarde réelle de 4 006 fichiers / 6 212 015 912 octets et
restauré sa copie dans un dossier isolé. La notice utilisateur contient encore une
commande lancée depuis `Magic/` qui échoue à l’import ; voir [audit](../audit.md).

## Étapes et fichiers

1. `backup_current` résout `_bibliotheque_mtg/v3-staging/current.json`,
   copie les exports de `versions/<id>/` et crée une copie SQLite cohérente via
   l'API de sauvegarde SQLite. Il omet `assets/` et les journaux WAL.
2. Il calcule taille et SHA-256 de chaque fichier copié, puis écrit
   `backup-manifest.json`.
3. `restore_backup` relit le manifeste, valide les fichiers non image, refuse une
   cible déjà existante et copie sous `<destination-root>/versions/<id>/`. Les
   anciens manifestes qui listent `assets/` restent restaurables si ce dossier a
   été supprimé de la sauvegarde ; il n'est jamais recopié.
4. Contrôler `catalogue.sqlite` restauré (`PRAGMA integrity_check`) et comparer
   son hash. Les slots d'image peuvent toujours porter le statut `ready` dans
   SQLite : les fichiers internes correspondants sont recréés depuis `Images/`
   au démarrage si cette collection est présente et les hashes concordent.
5. La restauration ne bascule pas `current.json`. Un changement de version active
   serait une opération distincte à préparer et vérifier.

## Invariants et cas d’échec

- Aucun fichier du fonds historique ni archive fournisseur n’est inclus
  automatiquement dans cette sauvegarde de version ; les préserver par un plan de
  sauvegarde séparé si nécessaire.
- Hash, taille ou fichier absent : la restauration doit échouer sans promouvoir
  une copie partielle.
- Destination déjà présente : choisir un nouvel emplacement ; ne pas écraser une
  sauvegarde sans inventaire.
- Sauvegarde interrompue ou prise pendant une image : vérifier le manifeste,
  l’intégrité SQLite et les assets ; refaire une copie calme si un contrôle
  échoue.

Voir [maintenance](../modules/maintenance-publication.manifest.md),
[stockage](../infra/stockage-local.manifest.md) et
[risques](../dependencies.risks.md).

## Decks personnels : récupération séparée

Les sauvegardes de catalogue ci-dessus n'incluent pas les decks personnels.
Conserver `_bibliotheque_mtg/state/personal-decks.sqlite` et le dossier
`_bibliotheque_mtg/state/personal-decks-backups/` dans une sauvegarde externe.
La copie `latest.sqlite` est une base autonome, produite par l'API SQLite de
sauvegarde après modification. La base principale contient également la table
`personal_deck_revisions`, avec les documents complets de chaque révision.

En cas d'incident, fermer le serveur, préserver les fichiers originaux (y compris
les éventuels `-wal`/`-shm`) et restaurer vers une destination NOUVELLE : depuis
`_bibliotheque_mtg/src/python`, appeler
`bibliotheque_mtg.personal_decks.restore_backup(chemin_sauvegarde, nouveau_chemin)`.
Cette fonction vérifie l'intégrité SQLite et refuse d'écraser une destination.
Contrôler les compositions de la copie avant de remplacer une base de production.
Aucune récupération automatique ne remplace silencieusement une base endommagée.
