# Workflow — télécharger ou réparer une image

## Déclencheur et résultat

Depuis la fiche d’une impression, l’utilisateur choisit « Télécharger l’image »
pour une face manquante ou « Vérifier ou réparer » pour un asset prêt. Un succès
produit un WebP local affichable, avec langue, dimensions et provenance visibles.

Depuis la visionneuse de carte, « Rechercher une meilleure image » demande
d'abord confirmation, puis prépare plusieurs variantes FR/EN distinctes de
l'image actuelle. Une seconde modale au-dessus de la fiche indique leurs dimensions d'origine et permet
d'agrandir chaque variante au survol, au focus ou au clic. Seule la validation
d'une variante sélectionnée publie un nouvel asset.
La recherche donne la priorité à la face affichée. Les téléchargements des
autres faces manquantes démarrent après la recherche en arrière-plan, sauf si
elle a été annulée.
Pendant un téléchargement groupé, la visionneuse peut rechercher et valider
une variante pour la face affichée. Si le lot a modifié cette face entre-temps,
la validation demande de relancer la recherche.

## Préconditions

- L’application locale, le snapshot actif et le `provider_card_id` exact sont
  disponibles.
- L’édition est éligible et possède un slot pour la face demandée.
- La demande en ligne est volontaire ; consultation et recherche n’effectuent pas
  de GET d’image.

## Étapes

1. `ui/app.js` envoie `POST /api/v2/download` avec `provider_card_id`,
   `face_index`, `task_id` et éventuellement `repair=true`.
2. `local_app.py` vérifie taille du corps, origine, champs et ID, puis appelle
   `image_download.download_printing` ; un autre POST peut demander l’annulation
   de la tâche.
3. Le module résout l’impression et lit uniquement les lignes source FR/EN/JA/PH qui lui
   appartiennent. Un slot déjà prêt est vérifié localement avant tout GET.
4. Les URL `image_uris` de la face sont tentées en FR puis EN, JA et PH, au plus trois
   GET par langue et face ; les URL Scryfall sont limitées à l'hôte HTTPS
   `cards.scryfall.io`. La relance approfondie peut
   aussi utiliser l’entrée exacte de `image_sources_manual.json`; son hôte HTTPS
   n’est permis que pour cette clé édition/numéro/face vérifiée.
   Si Scryfall ne fournit aucune URL française, le flux normal peut également
   consulter une entrée Wizards vérifiée par ID fournisseur exact ou par code
   d'édition, nom et numéro de collection dans la galerie produit officielle.
   Une simple correspondance de titre d'article reste une candidate de la loupe
   et n'est jamais publiée automatiquement.
5. La réponse doit être une image de taille bornée, décodable et de dimensions
   valides. La première bonne résolution disponible arrête la recherche ; sinon le
   meilleur candidat examiné est retenu.
6. Le fichier est converti en WebP, nommé par son SHA-256 et écrit dans la version
   active. `images.json`, `image_assets` et `image_slots` reçoivent le chemin, la
   langue et la provenance.
7. La fiche se recharge et sert l’asset par `/assets?path=...`. Le ledger dans
   `_bibliotheque_mtg/state/` conserve tentatives et reports de débit.

## Invariants et fichiers

- La [logique image](../modules/images.manifest.md) ne choisit jamais une
  impression par nom seul ; elle vérifie l’ID fournisseur, le `printing_id` et la
  face.
- Une copie valide donne `already_ready` sans réseau. Une copie corrompue exige
  `repair=true`; un slot manquant reste `missing` si aucun candidat n’est
  utilisable.
- Pas de redirection HTTP, de faux fichier HTML ou de dépassement des 20 MiB ; 403
  et 429 sont traités distinctement.
- Un succès doit relier le hash réel du WebP, son asset en base et le manifeste
  `images.json` de l’édition.

## Cas d’échec et reprise

- 429 : respecter le délai `Retry-After`; 403 : ne pas contourner le refus. Une
  coupure ou annulation garde le résultat traçable et le slot sans image de
  remplacement.
- Image trop petite ou illisible : essayer les autres candidats dans le budget,
  puis garder un statut manquant si rien n’est valide.
- Pour un WebP ancien dont l’ID commence par `asset_<lang>_`, le contrôle actuel
  peut signaler à tort une corruption ; voir [audit](../audit.md) avant de réparer
  en série.
- En cas d’échec durable, lire le résultat POST et le ledger, puis vérifier les
  URL et la provenance ; ne pas inventer une correspondance d’impression.
