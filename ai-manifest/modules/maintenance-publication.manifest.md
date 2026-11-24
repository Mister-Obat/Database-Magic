# Module — maintenance et publication

## Responsabilité

Actualiser les métadonnées sans collecte globale d’images, publier un nouveau
catalogue si les sources ont changé, conserver les assets encore liés, puis offrir
sauvegarde et restauration contrôlées.

## Emplacements

- `__main__.py` expose les sous-commandes CLI, dont `update`, `backup` et
  `restore`.
- `maintenance.py` contient `refresh_v2`, la conservation d’assets, la
  réutilisation des exports et les copies de sauvegarde.
- `v2/publish.py` résout et remplace atomiquement `v3-staging/current.json`.
- `version_history.py` énumère les versions V2 existantes et active une version
  antérieure après validation, sans recopier ses fichiers.
- `Mettre_a_jour_bibliotheque.bat` choisit l’environnement Python V2, le
  dossier de travail et la configuration.
- `tests_v2/test_maintenance_v2.py` et `test_publish_v23.py` portent les scénarios
  de changement, absence de changement et échec.

## Première initialisation

`Initialiser_Bibliotheque_Magic.bat` appelle `init` avec la configuration locale.
La commande prend un verrou, vérifie si le pointeur existe, puis collecte les
métadonnées, construit SQLite et les exports dans une version temporaire et
vérifie leur intégrité avant de publier le premier pointeur. Elle conserve une
version déjà active et refuse d'écraser un dossier de version. Aucun téléchargement
d'image ne fait partie de cette commande.

La commande `activate --version ID` permet d'activer une version restaurée depuis
le terminal avec le serveur arrêté. Elle prend le même verrou que `init`.

## Actualisation

1. `--dry-run` lit le pointeur et les rapports locaux, retourne une
   prévisualisation et n’utilise pas le réseau.
2. Une exécution réelle appelle `sources.run_sources(..., metadata_only=True)` et
   compare le hash All Cards ainsi que celui des séries au précédent.
3. Si les deux hashes concordent, l’état `no_change` ne publie rien et ne
   télécharge pas d’image.
4. Sinon un dossier temporaire sous `versions/` reçoit SQLite et les exports
   reconstruits. Les WebP précédents sont repris après vérification de hash et
   décodage ; les exports inchangés sont reliés ou copiés.
5. Le dossier reçoit un ID dérivé du hash SQLite ; `current.json` bascule par
   `os.replace` seulement après la construction.
6. Une exception avant le remplacement supprime le dossier temporaire et laisse
   l’ancien pointeur lisible.
7. La page Info de l’application locale peut déclencher la même actualisation,
   puis `refresh_deck_index` pour télécharger l’index MTGJSON des decks
   préconstruits. Les deux étapes sont suivies dans une tâche locale unique et
   leurs erreurs respectives sont rapportées sans masquer l’étape réussie.
8. La progression est publiée par étape : vérification des sources, construction
   du catalogue, conservation des images, mise à jour des manifestes,
   réutilisation des exports, publication, puis index des decks. Les pourcentages
   proviennent des octets téléchargés, des lignes ou cartes traitées et des
   fichiers examinés ; une étape sans objet est marquée `skipped`.
9. La tâche de la page Info accepte une annulation coopérative. Le bouton demande
   l'arrêt, les traitements vérifient cette demande entre lots, et la tâche
   devient `cancelled` après nettoyage du dossier temporaire. Le pointeur actif
   ne doit pas être modifié par une annulation demandée avant la publication ;
   une publication déjà achevée reste valide.
10. Le bouton de la page Info privilégie une vérification des séries et de leur
    nombre de cartes. Si aucun ajout n'est signalé, il conserve la version
    active sans télécharger All Cards. Si une série est nouvelle ou son nombre
    augmente, il récupère les cartes des seules séries concernées, ajoute les
    nouveaux IDs à l'archive locale et reconstruit une version atomique. Ce mode
    vise les ajouts ; le parcours CLI complet reste nécessaire pour répercuter
    suppressions et corrections de lignes existantes sans changement de nombre.
    Même lorsqu'aucune édition ne grandit, le rapport `official_french_text_coverage`
    est recalculé et transmis à l'interface; des comptes identiques ne masquent
    donc pas les traductions françaises manquantes. Les textes officiels vérifiés
    du sidecar sont importés séparément dans `official_french_text` sans changer
    les IDs Scryfall ni télécharger d'images.
11. Pour les ajouts, la construction ne doit traiter que les éditions concernées.
    Une copie SQLite cohérente de la version active reçoit les nouvelles lignes,
    les identités et les index de ces éditions ; les autres exports et assets
    sont réutilisés. Une vérification SQLite précède le remplacement atomique du
    pointeur. Le parcours complet CLI reste disponible comme resynchronisation.
12. L'historique de la page Info énumère les versions V2 déjà publiées. Activer
    une version antérieure vérifiée remplace seulement le pointeur actif ; la
    bascule refuse une mise à jour ou une écriture d'image en cours.
13. Après chaque nouvelle publication, conserver la version active et les deux
    versions publiées les plus récentes, selon la création de leur dossier.
    Supprimer les versions publiées plus anciennes ; ne jamais cibler `Images/`,
    les sauvegardes, les dossiers temporaires ou les anciens formats hors de
    l'historique. Un échec du nettoyage ne remet pas en cause la publication.

Toute actualisation lancée depuis l’Accueil comprend une étape Cardmarket
indépendante de la préférence d’affichage. Elle
consulte le guide officiel quotidien Magic et relie ses produits aux noms
canoniques du catalogue via les identifiants Cardmarket des lignes Scryfall.
Le prix de référence est la plus basse tendance positive parmi les impressions
et les variantes premium et non premium. La base globale `state/card-prices.sqlite` garde
un prix en centimes d’euro par nom, réutilisable dans les éditions et les
decks, y compris après activation d’une autre version du catalogue. La
transaction remplace les prix ensemble après lecture et validation du guide.
Un échec ou une annulation avant cette transaction préserve les prix existants.
Un ETag permet de réutiliser les prix si le guide, la version du catalogue
et la méthode de calcul restent identiques.

## Sauvegarde et restauration

- `backup_current` copie le catalogue SQLite par l'API de sauvegarde SQLite et
  les exports de la version désignée par `current.json`, sans `assets/` ni
  journaux WAL. Il crée `backup-manifest.json` avec taille et SHA-256 des fichiers
  conservés. `Images/` reste la collection durable séparée.
- `restore_backup` valide les fichiers non image du manifeste et copie vers
  `<destination-root>/versions/<version_id>` sans `assets/`, y compris pour une
  ancienne sauvegarde dont le dossier d'images a été retiré. Il ne change pas le
  pointeur de production. Les images d'une version réactivée peuvent être
  retrouvées depuis `Images/` au démarrage de l'application.
- La sauvegarde ne prend pas de verrou commun avec les écritures d’image : éviter
  un téléchargement simultané et contrôler la copie restaurée.

## Historique et retour de version

- `list_version_candidates(catalogue_root)` énumère les seuls dossiers `v2-<12
  caractères hexadécimaux>` directement sous `versions/`, avec taille et état
  structurel. Le calcul du poids additionne les tailles de fichiers sans les
  lire ni calculer de hash.
- `activate_version(catalogue_root, version_id)` ne copie rien : il valide le
  catalogue SQLite par `PRAGMA quick_check`, vérifie `editions/index.json` et
  ses chemins référencés, puis réutilise `atomic_publish_pointer`.
- L’appelant doit sérialiser l’activation avec les téléchargements d’images et
  les mises à jour. Une activation seule ne déclenche pas l'élagage ; il a lieu
  après une nouvelle publication.

## Invariants et dépendances

- Ne pas changer `current.json` avant qu’une base et ses exports existent dans la
  racine de publication.
- Ne pas télécharger d’images pendant `update`; les nouvelles faces arrivent avec
  le statut `missing`.
- Une publication peut être structurellement valide alors que la couverture de
  texte reste incomplète. Le rapport doit conserver cet état `incomplete` et ne
  peut annoncer 100 % sans texte officiel FR présent ou override revu.
- Conserver un ancien WebP seulement si son impression, sa face, son fichier, son
  hash et son décodage concordent. Les anciens IDs préfixés sont pris en charge
  par `_copy_forward_assets`.
- Dépend de [Scryfall](../infra/scryfall.manifest.md),
  [Wizards](../infra/wizards-gallery.manifest.md),
  [catalogue](catalogue.manifest.md), [images](images.manifest.md) et
  [stockage local](../infra/stockage-local.manifest.md).

## Points à vérifier

- Après mise à jour : résultat CLI, `current.json`, `PRAGMA integrity_check`,
  compteurs d’édition et d’enregistrement, exports concernés et images conservées.
- Après échec simulé : pointeur précédent octet pour octet et lecture de la base
  précédente.
- Avant restauration : destination nouvelle et espace libre ; après restauration :
  hashes du manifeste, intégrité SQLite et décodage d’images.
