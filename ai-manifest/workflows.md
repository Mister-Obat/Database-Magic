# Workflows : index des processus

Chaque lien ouvre un parcours avec déclencheur, étapes, fichiers impliqués,
invariants et cas d’échec. Les commandes doivent être exécutées depuis le dossier
indiqué dans le workflow ; `Magic/` et `_bibliotheque_mtg/src/python` n’ont pas le
même chemin d’import Python.

| Besoin | Processus | Quand le consulter |
|---|---|---|
| Utiliser la bibliothèque | [Démarrer et consulter](workflows/consulter-bibliotheque.md) | Lancer la page locale, chercher en FR/EN, filtrer, ouvrir une impression ou l’archive. |
| Ajouter de nouvelles éditions textuelles | [Actualiser le catalogue](workflows/actualiser-catalogue.md) | Exécuter le `.bat`, comprendre `--dry-run`, `no_change` ou une publication. |
| Obtenir ou réparer une image | [Image à la demande](workflows/telecharger-image.md) | Depuis une fiche, télécharger une face précise ou réparer un WebP endommagé. |
| Protéger et relire une version | [Sauvegarder et restaurer](workflows/sauvegarde-restauration.md) | Avant un changement de données ou lors d’un incident de stockage. |
| Relier les anciens fichiers | [Réconcilier le fonds historique](workflows/reconcilier-historique.md) | Auditer `Cartes Magic/`, les identités incertaines et les alias. |

## Ordre entre processus

- La [construction du catalogue](workflows/actualiser-catalogue.md) précède la
  consultation d’une nouvelle édition ; la recherche lit la version active
  indiquée par `current.json`.
- Un [téléchargement d’image](workflows/telecharger-image.md) demande une
  impression et un slot existants ; il ne reconstruit pas le texte.
- Une [sauvegarde](workflows/sauvegarde-restauration.md) capture la version active
  ; éviter de la lancer pendant un téléchargement d’image.
- Le [rapprochement historique](workflows/reconcilier-historique.md) produit des
  preuves et des statuts ; les fichiers ambigus restent dans le fonds d’origine.

Consulter [l’architecture](architecture.md) pour les frontières et
[l’audit](audit.md) pour les écarts encore ouverts.
