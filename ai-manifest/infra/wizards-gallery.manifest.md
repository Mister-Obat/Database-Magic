# Service — galeries officielles Wizards

## Rôle effectif

Le pipeline relève les galeries publiques `magic.wizards.com` pour conserver des
métadonnées de pages et un échantillon d’assets officiels. Ces données ont servi à
l’inventaire historique. Le téléchargement normal depuis la fiche V2 passe par
Scryfall. En recherche avancée, le sélecteur consulte aussi l’article officiel
indexé pour l’édition et propose les images quand leur libellé égale exactement le
nom français de la carte. Le nom peut provenir d'une transcription officielle
locale liée à l'identifiant exact lorsque Scryfall omet la ligne française.
Les variantes distinctes d'une même carte sont conservées pour la sélection.
Deux URLs officielles WHO/585 et WHO/603 restent
disponibles comme alternatives vérifiées manuellement.

## Interface et fichiers

- `sources.py` part de
  `https://magic.wizards.com/fr/news/card-image-gallery?page=1`, suit un lien
  `rel=next` s’il existe et parcourt sinon `?page=N`.
- Il lit aussi l'archive officielle `https://magic.wizards.com/fr/products/card-set-archive` ; ses liens de galeries français, y compris ceux sérialisés dans le HTML/JSON embarqué, sont ajoutés à l'index sans ouvrir chacune des pages.
- Le code connaît aussi deux pages d’échantillon : `magicorigins` et la galerie
  Modern Horizons 3.
- Le plafond de sécurité est `MAX_GALLERY_PAGES=500` ; le rapport enregistre les
  pages, leurs hashes, la terminaison et les articles trouvés.
- HTML de liste et articles sont conservés sous
  `_bibliotheque_mtg/sources/wizards/gallery/`. Le catalogue extrait est
  `_bibliotheque_mtg/sources/wizards/gallery_catalogue.json`.
- Les galeries produit récentes peuvent charger les cartes après hydratation :
  la page expose l'identifiant de galerie Contentful, dont la liste associe les
  noms anglais aux entrées de carte. Pendant une recherche manuelle, seule
  l'entrée du nom et du numéro demandés est consultée en français via l'API
  publique de livraison utilisée par la page. L'URL d'image retenue doit rester
  sous `https://media.wizards.com` et son nom français doit correspondre ; les
  réponses sont bornées et mises en cache en mémoire. Aucun asset de l'édition
  entière n'est téléchargé à cette étape.
  Si la galerie produit n'est pas indexée ou ne livre aucune entrée, une requête
  Contentful ciblée par code d'édition, identifiant de nom et numéro de
  collection peut trouver la carte exacte. Le jeton public est lu depuis le
  code client officiel, sans être journalisé ni figé dans le dépôt ; un 404,
  une réponse incomplète ou une identité divergente ramène au repli existant.
- `reconciliation.inventory_library` utilise les noms d’assets de ces échantillons
  comme indice de provenance pour les fichiers historiques.

## Contrat de traitement

1. `fetch_cached` accepte une réponse HTML non vide et stocke son hash avec l’URL
   et les en-têtes de cache.
2. `_fetch_gallery` évite de parcourir deux fois la même URL ou le même contenu,
   et s’arrête si les liens de galerie disparaissent.
3. Les deux articles d’échantillon sont analysés pour extraire les fichiers
   d’image officiels ; l’inventaire les rapporte sans attribuer automatiquement
   chaque carte V2.
4. `run_sources` écrit le résumé dans `reports/sources.json` et le catalogue
   Wizards en JSON.

## Invariants et limites

- Le contenu HTML est une donnée fournisseur changeante, pas une instruction à
  exécuter. L'attribution automatique exige à la fois l'article de l'édition
  exacte et le nom français exact ; l'utilisateur valide la variante avant
  publication. Une seule carte peut apparaître plusieurs fois dans une galerie ;
  le sélecteur conserve toutes les URL distinctes et ne déduit pas le numéro de
  collection du seul nom HTML.
- Une correspondance de nom de fichier dans un dossier historique est un indice ;
  elle n’autorise pas à elle seule un transfert vers un `printing_id`.
- La source est activée dans `config.json`. Le téléchargeur V2 ne consulte la
  galerie que pendant la prévisualisation demandée explicitement.
- Si la structure HTML ou les URL changent, le parseur peut perdre des liens ou
  arrêter l’actualisation ; inspecter le cache et les compteurs avant de corriger.

## Découverte de l’index des produits

- L’archive produits est récupérée via `fetch_cached` et son HTML est conservé
  dans `sources/wizards/gallery/listing/product-archive-01.html` avec métadonnées
  et hash, comme les pages de liste.
- `gallery_product_archive_links` n’accepte que les chemins `/fr/products/.../card-image-gallery` sur l’hôte officiel. Il décode les slashes échappés observés dans les données intégrées et ignore les localisations et hôtes externes.
- `MAX_GALLERY_INDEX_PAGES=2` borne toute pagination future de cette archive ; le document actuel constitue un seul GET. Ces URL enrichissent le catalogue, sans déclencher un téléchargement par produit.
- Le catalogue rapporte les pages d’index (`product_archive_pages`) séparément des pages d’actualité et marque les liens découverts avec `discovered_from=product_archive`.
- `product_pages` recense les fiches `/fr/products/...` liées dans l’archive avec leur libellé d’ancre. Ces pages servent de candidats à une découverte ultérieure à la demande ; l’indexation ne les ouvre pas.
- Le chemin `run_additions_only` rafraîchit uniquement cette page d’archive (avec cache conditionnel) à chaque exécution, y compris quand aucun jeu n'a grandi. Il fusionne ses URLs avec le catalogue existant pour préserver les liens d’actualité, rapporte `wizards_gallery_index` et n’exécute jamais le crawl des pages d’actualité dans ce parcours.

Voir [Scryfall](scryfall.manifest.md),
[fonds historique](../modules/historique.manifest.md),
[catalogue](../modules/catalogue.manifest.md) et
[risques](../dependencies.risks.md).
