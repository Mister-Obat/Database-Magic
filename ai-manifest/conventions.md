# Conventions applicables

Ces règles sont constatées dans le code et les schémas de l’installation V2. Les
choix seulement souhaités, mais encore absents, sont listés dans
[l’audit](audit.md).

## Données et identités

- Le contrat V2 est `2.0.0` dans `v2/query.py`, `v2/publish.py` et les exports ;
  la racine physique porte le nom historique `v3-staging`.
- Le catalogue stocke une ligne `source_records` par identifiant de carte du
  fournisseur, avec langue et JSON brut. Les identités d’impression entre langues
  reposent sur des preuves, jamais sur le seul nom.
- Une exception d’identité reste explicite dans `identity_exceptions`. Un fichier
  ancien ambigu reste dans l’archive sans attribution forcée.
- Les fichiers d’édition ont les noms `index.json`, `index.md` et `images.json`.
  JSON est l’export structuré ; Markdown facilite la lecture humaine.
- Le statut du texte et celui des images sont distincts. Les slots `missing` n’ont
  pas d’image blanche de remplacement.

## Publication et stockage

- Construire les nouvelles données dans une version temporaire ; contrôler SQLite
  et les chemins ; publier ensuite `current.json` par `os.replace`.
- Les chemins servis par l’API sont résolus à l’intérieur d’une racine connue. Ne
  jamais interpréter directement un chemin reçu du navigateur.
- Les mises à jour réutilisent les exports identiques et les WebP existants
  seulement après contrôle de hash et de décodage.
- Les images V2 téléchargées sont des WebP rangés sous
  `assets/webp/<langue>/<sha256>.webp`. La provenance et les dimensions restent en
  base.
- Les mêmes images prêtes sont classées sous `_bibliotheque_mtg/Images/<édition>/`
  avec numéro, nom et face. Le catalogue garde les chemins internes par hash ; le
  dossier organisé peut restaurer un fichier interne absent après contrôle de hash.
- Un original historique n'est jamais supprimé automatiquement. Un nettoyage
  explicitement demandé exige une identité vérifiée, le SHA-256 de l'original et
  un WebP organisé valide de 531×738.

## Réseau et interface

- Le serveur HTTP est lié à `127.0.0.1`; `MAGIC_LIBRARY_PORT` change le port, avec
  8765 par défaut.
- La consultation passe par des GET locaux. Une demande d’image passe par
  `POST /api/v2/download` depuis la fiche et peut être annulée ; le contrôle
  d’origine refuse un autre navigateur distant.
- Le téléchargeur V2 n’autorise en production que les URL HTTPS de
  `cards.scryfall.io` ; il refuse les redirections et borne taille, durée et
  nombre de GET.
- La priorité d’image codée est FR puis EN, au plus trois GET par langue et par
  face. Une bonne résolution FR termine la recherche ; sinon le meilleur candidat
  disponible est retenu selon le code.

## Travail sur le code et la documentation

- `Magic/Prog` et `_bibliotheque_mtg/src/python` sont deux arbres distincts.
  Commencer par identifier le parcours concerné ; les lanceurs utilisateur ciblent
  le second.
- La suite V2 se trouve dans `tests_v2/`, les tests antérieurs dans
  `tests_bibliotheque/` et `test_regles.py`.
- La racine est un dépôt Git (`git rev-parse --show-toplevel` renvoie ce dossier).
  Aucun fichier de configuration de formatage ou de lint n’a été observé à la
  racine ; ne pas inventer de règle de branche, commit ou outil automatique.
- `AGENTS.md` existe à la racine et impose de lire d’abord `ai-manifest/index.md`,
  puis les manifests concernés avant toute modification du programme. Cette
  documentation existait déjà au début de la présente vérification ; ses fichiers
  étaient toutefois ignorés par Git, situation corrigée dans `.gitignore`.
