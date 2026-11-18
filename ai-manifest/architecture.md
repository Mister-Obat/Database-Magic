# Architecture et invariants

Le projet juxtapose un pipeline historique, un catalogue textuel V2 et une
application locale. Le pointeur de publication V2 est la frontière entre
construction et consultation.

## Frontières

| Zone | Rôle | Frontière observable |
|---|---|---|
| Sources distantes | Scryfall fournit les séries et All Cards ; Wizards fournit des métadonnées de galeries. | Les réponses et l’archive sont conservées dans `_bibliotheque_mtg/sources/`. |
| Import de sources | `sources.py` valide l’archive gzip JSONL et produit la base source `_bibliotheque_mtg/catalogue.sqlite`. | Rapport `reports/sources.json` et politique d’éligibilité. |
| Construction V2 | `v2/build_catalogue.py` et `v2/catalogue.py` créent SQLite, FTS5 et exports par édition. | Nouvelle version sous `v3-staging/versions/`. |
| Publication | `v2/publish.py` valide les chemins et remplace atomiquement `current.json`. | Les lecteurs résolvent toujours la version pointée. |
| Application | `local_app.py`, `v2/api.py`, `v2/query.py` et `ui/` servent uniquement `127.0.0.1`. | Recherche sur SQLite en lecture seule ; téléchargement par action POST explicite. |
| Images | `image_download.py` valide la source exacte, le contenu, les dimensions et la langue ; convertit en WebP. `organized_images.py` classe ensuite chaque image prête par édition. | Le téléchargement modifie l’asset, le slot SQLite, `images.json` de la version courante et le miroir `_bibliotheque_mtg/Images/`. |
| Fonds historique | `Cartes Magic/`, `reconciliation.py` et les anciens modules gardent les originaux et leurs rapprochements. | Les correspondances incertaines ne sont pas attribuées. |

## Flux principal

`Scryfall/Wizards → sources/ → catalogue.sqlite source → build V2 → versions/<id>/ → current.json → API locale → interface`.

Pour les images :
`fiche d’impression → URL Scryfall autorisée → validation du contenu → WebP → image_assets/image_slots + images.json → Images/<édition>/ → /assets`.
La recherche ordinaire lit le catalogue local et ne sollicite aucune source
distante.

## Identités

- `editions` conserve les séries éligibles et différées. Une édition différée ne
  paraît pas dans la liste de recherche.
- `source_records.provider_card_id` identifie chaque ligne fournisseur.
  `printings.printing_id` regroupe plusieurs langues seulement si série, numéro,
  oracle/illustration, layout et structure des faces apportent une preuve
  suffisante.
- Les collisions et preuves manquantes restent séparées, avec
  `identity_exceptions` ; les alias historiques ont leur propre statut dans
  `legacy_aliases`.
- `card_faces` représente les faces physiques. Les autres faces textuelles d’un
  même objet n’impliquent pas forcément plusieurs images.

## Invariants à maintenir

- `current.json` doit pointer vers une base et un répertoire d’éditions existants,
  sous la racine de publication ; il est remplacé après validation.
- Un slot `ready` référence un asset ; `missing` ou `pending_review` ne référence
  pas de fichier. Le schéma SQL impose cette relation.
- Le `provider_card_id`, le `printing_id` et l’index de face restent liés jusqu’au
  WebP ; aucun rapprochement par simple nom de carte.
- L’archive All Cards est contrôlée par taille, hash et décompression avant
  reconstruction.
- Les assets sont nommés par hash de contenu ; les exports V2 sont produits dans
  un ordre déterministe.
- Les originaux de `Cartes Magic/` et les archives source restent séparés du
  catalogue géré jusqu'à une reprise et un nettoyage explicitement demandés.

La version de métadonnées est publiée par pointeur. Les téléchargements d’image
écrivent ensuite dans cette version active : la copie d’un snapshot pendant un
téléchargement demande donc une précaution particulière, détaillée dans les
[risques](dependencies.risks.md).

Voir [catalogue](modules/catalogue.manifest.md),
[application](modules/interface-recherche.manifest.md),
[images](modules/images.manifest.md) et
[maintenance](modules/maintenance-publication.manifest.md).
