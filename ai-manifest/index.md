# AI manifest — bibliothèque Magic locale

Ce dossier documente le projet. L’application active
cherche les cartes dans un catalogue textuel V2 local et récupère leurs images
WebP depuis la fiche, à la demande. Le fonds `Cartes Magic/` et le téléchargeur de
`Magic/Prog` restent des parcours historiques distincts.

## Règles de lecture

1. Lire l’[AGENTS.md racine](../AGENTS.md) pour les règles de travail, puis
   utiliser cette table des matières pour ouvrir seulement les documents utiles.
   Les chemins détaillés vivent dans `modules.map.md`.
2. Traiter le pointeur `current.json` comme le point d’entrée du catalogue
   actif ; ne pas déduire la version active d’un nom de dossier.
3. Distinguer les **faits de code**, les résultats datés dans les rapports, et les
   questions de [l’audit](audit.md). Les chiffres changent après chaque mise à
   jour.
4. Les rapports de `_bibliotheque_mtg/reports/` sont des preuves historiques. Les
   consulter après le manifest pertinent, sans charger toute l’archive d’emblée.
5. Avant une modification du programme, lire le manifest du module concerné et
   mettre à jour la documentation si le contrat observé est incomplet ou ambigu.

## Vue du projet

- [Carte fonctionnelle](carto.md) — à lire pour comprendre ce que l’utilisateur
  peut lancer et ce que produit chaque parcours.
- [Architecture](architecture.md) — à lire avant de toucher aux frontières entre
  source, SQLite, versions, serveur et images.
- [Conventions](conventions.md) — à lire avant une modification d’identité, de
  fichier, de route ou de publication.
- [Stack et commandes](stack.md) — à lire pour retrouver l’environnement Python,
  la configuration et les commandes dont l’usage a été vérifié.
- [Risques des dépendances](dependencies.risks.md) — à lire avant un import
  volumineux, un changement de fournisseur ou une sauvegarde.
- [Audit des écarts](audit.md) — à lire pour les bugs observés, les contradictions
  de notice et les questions encore ouvertes.

## Se repérer dans le code

- [Modules : liste courte](modules.list.md) — à lire pour choisir le composant
  responsable d’un symptôme.
- [Modules → fichiers](modules.map.md) — à lire lorsqu’il faut ouvrir le code ou
  les tests précis.
- [Catalogue et identités](modules/catalogue.manifest.md) — à lire pour séries,
  lignes source, impressions, langues, faces et exports.
- [Interface et recherche](modules/interface-recherche.manifest.md) — à lire pour
  l’API locale, les filtres, fiches et routes HTTP.
- [Direction artistique de l’interface](modules/direction-artistique.manifest.md)
  — à lire avant de modifier la palette, les boutons ou la mise en page.
- [Images à la demande](modules/images.manifest.md) — à lire pour sélection FR/EN,
  budgets, WebP, provenance et réparation.
- [Génération d’illustration](modules/card-generation.manifest.md) — à lire pour
  génération IA locale de candidates PNG et leur séparation du catalogue.
- [Maintenance et publication](modules/maintenance-publication.manifest.md) — à
  lire pour `update`, le pointeur actif et les copies de sauvegarde.
- [Fonds historique](modules/historique.manifest.md) — à lire pour les anciens
  PNG, `Cartes.md`, les alias et les rapprochements prudents.

## Fournisseurs et infrastructure

- [Scryfall](infra/scryfall.manifest.md) — à lire pour l’archive All Cards, les
  séries, le cache et les URL d’images actives.
- [Galeries Wizards](infra/wizards-gallery.manifest.md) — à lire pour le relevé
  HTML officiel et ses limites dans le parcours V2.
- [Stockage local](infra/stockage-local.manifest.md) — à lire pour distinguer
  fonds historique, sources, version active, ledger et sauvegardes.

## Exécuter un processus

- [Index des workflows](workflows.md) — à lire pour choisir le parcours
  opérationnel avant d’exécuter une commande.
- [Démarrer et consulter](workflows/consulter-bibliotheque.md) — à lire pour
  lancer le `.bat`, chercher, filtrer et ouvrir une fiche.
- [Actualiser le catalogue](workflows/actualiser-catalogue.md) — à lire pour le
  `--dry-run`, l’actualisation réelle et ses cas d’échec.
- [Télécharger ou réparer une image](workflows/telecharger-image.md) — à lire
  avant un GET image depuis la fiche d’une impression.
- [Sauvegarder et restaurer](workflows/sauvegarde-restauration.md) — à lire avant
  une copie, une reprise ou une vérification de hashes.
- [Réconcilier le fonds historique](workflows/reconcilier-historique.md) — à lire
  avant d’attribuer un ancien fichier à une carte V2.

## Sources de vérité datées

Le [rapport de validation V2](../_bibliotheque_mtg/reports/validation_v2.md) et la
[notice utilisateur](../README_Bibliotheque_Magic.md) documentent l’état du
21 octobre 2026. Leurs compteurs n’engagent pas les futures versions ; lire
`current.json` et l’API locale pour l’état courant.

Le dossier `ai-manifest/` est une documentation de projet, pas une donnée
d’exécution. Ses fichiers peuvent désormais être suivis par Git ; les rapports
volumineux et les données locales restent hors de cet index.
