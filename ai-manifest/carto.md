# Carte fonctionnelle

État de fonctionnement révisé le 16 novembre 2026. Le parcours actuellement lancé par les deux
`.bat` de `Magic/` est celui de `_bibliotheque_mtg/src/python`. Le code dans
`Magic/Prog` sert encore au téléchargement historique et aux tests antérieurs.

## Ce que fait le système

1. Il archive les métadonnées Scryfall All Cards et les séries, puis relève les
   pages des galeries Wizards.
2. Il construit un catalogue SQLite V2 avec éditions, impressions, lignes source
   dans leurs langues, traductions, faces physiques et emplacements d’image.
3. Il exporte pour chaque édition éligible `index.json`, `index.md` et
   `images.json` dans la version publiée.
4. Il sert un logiciel local sur `127.0.0.1:8765` : éditions, recherche, filtres,
   fiches et archive historique.
5. Depuis une fiche, il peut récupérer l’image de l’impression et de la face
   choisies, préférer le français, convertir en WebP, enregistrer la provenance
   et classer l'image sous `_bibliotheque_mtg/Images/<édition>/`.
6. Il peut actualiser les métadonnées, sauvegarder la version active et restaurer
   une copie dans un dossier isolé.

## Entrées et sorties

| Entrée | Destination / usage |
|---|---|
| `Lancer_Bibliotheque_Magic.bat` | Lance le serveur local V2 et ouvre son interface. |
| `Mettre_a_jour_bibliotheque.bat` | Actualise le texte et les éditions ; `--dry-run` prévisualise sans réseau. |
| `Magic/Cartes.md` | Liste du téléchargeur historique de `Magic/Prog`, indépendante de la recherche V2. |
| `_bibliotheque_mtg/config.json` | Politique des sources, des images, du réseau et des chemins de l’ancien pipeline. |
| `_bibliotheque_mtg/v3-staging/current.json` | Pointeur vers la base et les exports V2 utilisés par l’interface. |
| `_bibliotheque_mtg/Images/` | Collection navigable des images prêtes, organisée par édition. |
| `Cartes Magic/` | Fonds historique restant à identifier et métadonnées issues du parcours antérieur. |

## Lecture des données

Les éditions éligibles du snapshot actif se trouvent dans les exports de la
version pointée par `_bibliotheque_mtg/v3-staging/current.json`. Les dossiers
de `Cartes Magic/` représentent le fonds historique restant ; les images prêtes
sont consultables par édition dans `_bibliotheque_mtg/Images/`.

Les compteurs de cartes, éditions, decks et images sont calculés par `/api/v2/stats`
sur la version active. Ils changent à chaque import ou actualisation et ne sont pas
figés dans ce document.

## Où aller ensuite

- [Architecture](architecture.md) : frontières entre sources, base, application et
  fichiers.
- [Modules](modules.list.md) : responsabilités du code.
- [Workflows](workflows.md) : parcours de démarrage, actualisation, image et
  restauration.
- [Audit](audit.md) : écarts présents dans le code ou la documentation.
