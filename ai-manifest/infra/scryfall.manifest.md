# Service — Scryfall

## Rôle effectif

Scryfall est la source du catalogue textuel V2 et des URL d’image actuellement
utilisées par le bouton de téléchargement. Le code consulte le descripteur All
Cards, les pages `/sets`, puis l’archive JSONL compressée ; les images d’une
impression sont recherchées dans les `image_uris` des lignes FR/EN de cette même
impression.

## Interfaces et cache local

- `sources.py` utilise `https://api.scryfall.com/bulk-data` pour le descripteur et
  `https://api.scryfall.com/sets` pour les séries. L’URL de l’archive provient du
  descripteur, généralement sur `data.scryfall.io`.
- La mise à jour ciblée de la page Info lit `/sets`, compare les éditions et
  leurs nombres de cartes au catalogue publié, puis utilise `/cards/search`
  avec `unique=prints`, `include_multilingual=true` et `include_extras=true`
  pour les seules éditions nouvelles ou agrandies. Leurs cartes complètes sont
  construites en SQLite localement ; les éditions inchangées sont conservées.
  Cette voie ne détecte pas les corrections sans changement de nombre : le
  parcours All Cards complet reste disponible en CLI.
- Les réponses de métadonnées sont archivées sous
  `_bibliotheque_mtg/sources/scryfall/` avec fichiers `.meta.json`, ETag ou
  Last-Modified si disponibles.
- `download_bulk` réutilise l’archive quand la date du descripteur, sa taille et
  son hash local concordent. Sinon il écrit un `.part`, contrôle taille et JSONL
  gzip, puis remplace l’archive.
- `image_download.py` autorise en production seulement `https://cards.scryfall.io`
  pour les images et ne suit pas les redirections. La relance approfondie peut
  faire un GET ciblé sur `/cards/<set>/<collector_number>/fr` pour découvrir une
  ligne FR ajoutée depuis le snapshot; le résultat est vérifié contre l’identité
  locale et ne modifie pas le catalogue.
- La source datée et son SHA-256 sont repris dans `reports/sources.json` et
  `catalogue_meta`.

## Flux et budgets

1. Le client de métadonnées espace les appels `api.scryfall.com` selon
   `config.json` (600 ms observés) et distingue erreurs de transport, 403, 429 et
   5xx.
2. Le descripteur est validé avant de décider un téléchargement d’archive. Chaque
   ligne JSONL doit être un objet carte avec ID et langue.
3. Les éditions futures ou exclues par politique restent différées ; toutes les
   autres lignes source alimentent le catalogue V2.
4. Le bouton d’image lit les URL de cette impression dans l’ordre des tailles
   définies par `image_download.py` et borne à trois GET par langue et par face.
   La relance approfondie ajoute au plus une requête ciblée de métadonnées FR,
   espacée d’au moins 600 ms entre les appels et sans suivre de redirection.

## Invariants

- Ne jamais déduire qu’une image d’un nom similaire correspond à la même
  impression ; l’ID de carte source, le `printing_id` et la face sont requis.
- Une archive tronquée ou dont le SHA diffère ne doit pas remplacer le catalogue
  publié.
- Le téléchargement d’images reste séparé de l’actualisation de métadonnées.
- La requête image approfondie ne remplace pas le snapshot du catalogue; seule
  l’URL live de la carte correspondante peut alimenter le slot identifié.
- En cas de 429, différer selon `Retry-After` ; ne pas tenter de contourner un
  403.

## Vérifications et limites

- Après une actualisation, contrôler les empreintes dans `reports/sources.json`,
  le nombre de séries éligibles, les IDs importés et les exports par édition.
- Après une image, comparer le hash du WebP, la langue choisie et l’URL source
  dans la provenance.
- Une URL d’image obsolète laisse le slot manquant ou en erreur ; la recherche
  textuelle locale reste disponible.
- Voir [catalogue](../modules/catalogue.manifest.md),
  [images](../modules/images.manifest.md),
  [actualisation](../workflows/actualiser-catalogue.md) et
  [risques](../dependencies.risks.md).
