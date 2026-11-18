# Workflow — actualiser le catalogue textuel

## Déclencheur et résultat

Exécuter `Mettre_a_jour_bibliotheque.bat` pour intégrer de nouvelles
éditions et lignes Scryfall. Le résultat est `no_change` avec la version courante,
ou une nouvelle version V2 publiée par `current.json`. L’actualisation ne collecte
pas les images manquantes. Depuis l’application, le bouton de la page Info vérifie
les éditions nouvelles ou agrandies via l’API Scryfall, construit seulement ces
éditions, les fusionne dans une copie du catalogue local puis actualise l’index
des decks préconstruits. Il affiche les étapes et permet l’annulation. Le parcours
CLI reste la resynchronisation complète des corrections anciennes.

## Préconditions

- Configuration `_bibliotheque_mtg/config.json`, environnement Python isolé et
  version V2 courante accessibles.
- Espace suffisant pour archive source, base temporaire et nouvelle version ;
  anciens originaux conservés.
- Avant un changement majeur, disposer d’une
  [sauvegarde vérifiée](sauvegarde-restauration.md).

## Étapes

1. Depuis la racine, exécuter
   `& '.\Mettre_a_jour_bibliotheque.bat' --dry-run`. Ce mode lit l’état
   local, sans requête réseau ni publication ; le contrôle observé a retourné le
   code 0.
2. Lancer le même `.bat` sans `--dry-run` pour l’actualisation réelle. Le lanceur
   se place dans `_bibliotheque_mtg/src/python`, choisit la configuration et
   exécute `python -m bibliotheque_mtg --config ... update`.
3. `sources.run_sources` relève le descripteur All Cards, vérifie/réutilise ou
   télécharge l’archive, importe les séries, relève les galeries et enregistre
   `reports/sources.json`.
4. `refresh_v2` compare les hashes de l’archive et des séries aux métadonnées de
   la version active. S’ils n’ont pas changé, il retourne `no_change`.
5. S’il y a changement, `v2/build_catalogue.py` construit SQLite et les index sous
   un nouveau dossier temporaire. La maintenance vérifie les anciens WebP, met à
   jour les manifestes d’image et réutilise les exports identiques.
6. Le dossier final reçoit son ID de version ; `v2/publish.py` remplace
   `current.json` atomiquement. La nouvelle recherche utilise alors la base
   pointée.
7. Après publication, le programme conserve la version active et les deux
   versions publiées précédentes les plus récentes. Il supprime les versions
   publiées plus anciennes ; un échec d'élagage est signalé sans annuler la
   publication.

## Invariants et fichiers

- [Sources](../infra/scryfall.manifest.md),
  [catalogue](../modules/catalogue.manifest.md) et
  [maintenance](../modules/maintenance-publication.manifest.md) constituent la
  chaîne ; [Wizards](../infra/wizards-gallery.manifest.md) fournit le relevé de
  galeries.
- Une archive tronquée ou un build en échec laisse le pointeur précédent
  consultable.
- Les nouvelles faces ont un slot `missing`; aucune image n’est téléchargée par ce
  flux.
- Un ancien WebP n’est repris que si impression/face et contenu correspondent ; la
  validation et le pointeur précèdent la lecture par l’application.

## Vérifications et échecs

- Après `no_change`, comparer `version_id` avant/après et confirmer
  `image_downloads=0`, `changes_published=0`.
- Après nouvelle version, vérifier `PRAGMA integrity_check`, IDs éligibles,
  compteurs d’édition, `editions/index.json` et quelques `images.json` ; conserver
  l’ancienne version jusqu’à validation.
- 403, 429 ou transport indisponible : lire le code et `reports/sources.json` (mise à jour complète) ou `reports/additions-update.json` (ajouts ciblés) ;
  attendre ou réessayer sans contourner la source. Le pointeur précédent reste la
  référence.
- Si la source est inchangée mais qu’une image doit être ajoutée, suivre
  [l’image à la demande](telecharger-image.md) depuis sa fiche.
