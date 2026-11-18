# Dépendances structurelles et risques

Ce suivi existe parce que la bibliothèque dépend d’archives distantes
volumineuses, de plusieurs états locaux liés et d’un catalogue vivant pendant le
téléchargement d’images. Les limites ci-dessous sont déduites du code et des
contrôles du 21 octobre 2026.

| Dépendance | Risque observable | Mesure existante / contrôle à garder |
|---|---|---|
| Scryfall All Cards et `/sets` | Une archive modifiée, tronquée ou indisponible empêche un catalogue textuel à jour. | Taille, SHA-256, décompression JSONL, cache des réponses et ancien pointeur conservé en cas d’échec. |
| Pages de galerie Wizards | Un changement HTML ou un refus HTTP peut interrompre `run_sources`, même si l’archive Scryfall est valide. | Limite de pages, détection de contenu répété, HTML archivé et rapport source ; mesurer l’échec avant une relance. |
| URLs d’image `cards.scryfall.io` | URL expirée, 403, 429, erreur de contenu ou image trop petite. | URL autorisée, pas de redirection, GET bornés, état manquant explicite ; nouvelle source éventuelle à valider séparément. |
| SQLite V2 + exports + assets | Une écriture partielle pourrait désaligner base, `images.json` et fichier WebP. | Publication atomique du pointeur et du WebP, transaction SQLite, vérification de hash/décodage ; inspection après incident. |
| Espace disque | Archive source, base de plusieurs Go, versions et sauvegardes coexistantes. | Vérifier l’espace avant gros import et garder les sauvegardes hors du parcours de lecture ; aucun élagage automatique prouvé pour V2. |
| Configuration absolue `D:` | Un déplacement du projet casse les chemins de `config.json`. | Ajuster les chemins et refaire `--dry-run`, puis valider le pointeur avant mise à jour réelle. |
| Fonds historique | Des doublons, images corrompues ou identités ambiguës peuvent créer une attribution trompeuse. | Inventaire par hash, statuts d’identité incertaine, originaux conservés. |

## Risques de cohérence à suivre

1. **Sauvegarde pendant un téléchargement.** `backup_current` utilise `copytree`
   sur la version active et ne prend pas le verrou du téléchargeur. Faire la copie
   lorsque l’interface ne télécharge pas ; contrôler ensuite hashes et
   `PRAGMA integrity_check`.
2. **Version active enrichie après publication.** La publication des métadonnées
   se fait par pointeur atomique, puis une image à la demande modifie SQLite,
   `images.json` et `assets/` dans cette même version. Une règle d’immuabilité
   absolue du dossier serait inexacte.
3. **Conservation des anciens assets.** Les anciens IDs d’image peuvent préfixer
   le hash ; `maintenance.py` sait les reprendre. Le vérificateur de réparation de
   `image_download.py` compare encore l’ID complet au hash nu : voir
   [audit](audit.md).
4. **Budget durable d’image.** Le ledger garde les GET par impression, face et
   langue. Aucun reset explicite n’a été observé dans `download_printing` ;
   préciser la politique de relance après épuisement.

## Déclencheurs de vérification

- Après une mise à jour : lire `current.json`, la base active et
  `reports/sources.json`, puis comparer les compteurs et les exports.
- Après une demande d’image : lire le statut du slot, la langue choisie, le hash
  du WebP et son `images.json`.
- Après une restauration : vérifier le manifeste de sauvegarde, l’intégrité SQLite
  et un échantillon d’images, puis seulement décider d’un changement de pointeur.
- Après un changement de fournisseur : réévaluer les budgets, la provenance et les
  erreurs 403/429 avant d’activer sa collecte.

Voir [Scryfall](infra/scryfall.manifest.md),
[Wizards](infra/wizards-gallery.manifest.md),
[stockage local](infra/stockage-local.manifest.md) et les
[workflows](workflows.md).
