# Modules : liste courte

Un module ci-dessous correspond à une responsabilité qui demande plusieurs
fichiers ou possède des invariants à préserver. Les scripts ponctuels de `work/`
et les rapports ne sont pas traités comme des modules applicatifs.

## Parcours V2 actif

1. [Catalogue et identités](modules/catalogue.manifest.md) — acquisition des
   métadonnées, construction SQLite et exports par édition. À lire pour toute
   modification des séries, langues, faces ou index.
2. [Interface et recherche](modules/interface-recherche.manifest.md) — serveur
   HTTP local, API, requêtes et interface. À lire pour les filtres, fiches, routes
   ou problèmes de navigation.
3. [Images à la demande](modules/images.manifest.md) — choix de source, limites
   réseau, conversion WebP et état des slots. À lire avant tout changement de
   téléchargement ou de réparation.
4. [Maintenance et publication](modules/maintenance-publication.manifest.md) —
   actualisation, copie des assets, pointeur, sauvegarde et restauration. À lire
   avant toute opération sur les versions.

5. [Decks préconstruits](modules/decks-preconstruits.manifest.md) — import JSON local, stockage SQLite séparé et résolution facultative des cartes.

6. [Génération d’illustration](modules/card-generation.manifest.md) — candidates
   d’illustrations personnalisées produites via Codex CLI, sans publication dans
   le catalogue.

## Compatibilité historique

7. [Fonds historique et rapprochement](modules/historique.manifest.md) —
   inventaire de `Cartes Magic/`, alias, anciens collecteurs et tests. À lire pour
   relier un fichier ancien à une impression V2 ou comprendre le code antérieur.

## Choisir rapidement

- Une **carte absente de la recherche** : catalogue → interface ; vérifier d’abord
  l’éligibilité de l’édition et l’ID source.
- Une **image absente ou dans la mauvaise langue** : images ; consulter ensuite le
  slot et la provenance dans le catalogue.
- Une **mise à jour qui n’apparaît pas** : maintenance → catalogue ; vérifier
  `current.json` et l’archive source.
- Un **ancien PNG non reconnu** : fonds historique ; conserver le fichier si
  l’identité n’est pas démontrée.

La [correspondance modules → code](modules.map.md) est le point unique pour
localiser rapidement les fichiers. Les [workflows](workflows.md) donnent ensuite
l’ordre d’exécution des parcours importants.
