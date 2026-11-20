# Audit : écarts et questions ouvertes

Constats initialement consignés le 21 octobre 2026 et revalidés pendant la
maintenance documentaire du même jour. Ce document distingue les limites voulues
de la V2 des écarts du code ou de la notice. Les rapports détaillés restent dans
`_bibliotheque_mtg/reports/`, hors du parcours de lecture normal.

## Écarts observés à corriger lors d’une future intervention

| Priorité | Constat vérifié | Conséquence / point de contrôle |
|---|---|---|
| Haute | `maintenance._copy_forward_assets` accepte l’ID historique `asset_<lang>_<sha256>`, mais `image_download.download_printing` compare `sha256(fichier)` directement à `asset_id` pour un slot déjà prêt. | Les anciens WebP repris peuvent être déclarés corrompus par « Vérifier ou réparer » malgré un fichier valide. Adapter le contrôle aux deux formats et vérifier le clic sur un asset repris. |
| Haute | `backup_current` copie l’arborescence avec `shutil.copytree` alors que `download_printing` modifie le SQLite et `images.json` de la version active. | Une sauvegarde lancée pendant une image peut être incohérente. Définir une coordination ou une copie SQLite cohérente ; jusque-là éviter le chevauchement et contrôler la restauration. |
| Moyenne | `/api/v2/stats` renvoie `catalogue_date: null` sur le snapshot actif. `local_app._stats` lit `generated_at_utc` ou `snapshot_date`, tandis que la construction V2 écrit `source_as_of`. | La fraîcheur du catalogue n’apparaît pas dans l’interface. Relier le bon champ et confirmer la date dans l’API. |
| Moyenne | Le `.bat --dry-run` réel renvoie `cached_source_as_of: null` ; `refresh_v2` lit `archive_as_of` depuis `catalogue_scope.json`. | L’aperçu omet une date pourtant présente ailleurs. Définir la source exacte de fraîcheur et vérifier l’affichage. |
| Moyenne | La commande de sauvegarde publiée dans `Magic/README_Bibliotheque_Magic.md`, exécutée depuis `Magic/`, échoue : `No module named bibliotheque_mtg`. | Exécuter le CLI depuis `_bibliotheque_mtg/src/python` ou régler `PYTHONPATH`, puis corriger la notice. Le [workflow de restauration](workflows/sauvegarde-restauration.md) donne le chemin fonctionnel. |
| Moyenne | `tests_v2/test_deck_app.py::test_deck_routes_and_import_use_exact_provider_ids` corrige `local_app.deck_cover_paths`, symbole déplacé depuis dans `deck_media.py`. | Test périmé (échec préexistant, hors archives). À réécrire contre `deck_media.deck_cover_paths`. |

## Tests périmés corrigés (17 novembre 2026, passe archives)

- `test_local_app.test_archive_manual_hash_and_lens_result` simulait encore
  l'ancien upload direct Google Lens (`app.requests`, `result_url`). Réécrit sur
  le contrat actuel ticket + bridge (ticket hexadécimal à usage unique, pont HTML
  avec `Content-Security-Policy`, 400 si ticket inconnu ou déjà consommé).
- `test_local_app.test_local_same_oracle_french_fallback_requires_compatible_identity`
  attendait l'ancien message « Texte officiel français indisponible » ; aligné
  sur le message actuel (« aucun texte français officiel »).
- `bibliotheque_mtg/tests/test_ocr_candidates.test_proposes_only_single_composite_match`
  aligné sur la cible OCR enrichie (`face_index` + `title` après l'OCR conscient
  des faces) ; deux tests ajoutés couvrant verso (face 1) et recto (face 0).
- L'expansion FR/EN d'archive (`_oracle_language_expansion`) exécute des
  requêtes à une table indexée par oracle : avec un `IN` multi-valeurs, SQLite
  basculait sur un scan complet de `source_records` (≈26 s par recherche) en
  l'absence de statistiques ANALYZE dans le catalogue.
- Premier jet du nom automatique : l'OCR n'exposait `ocr_title` que pour les
  statuts uniques, si bien que l'analyse OCR ne proposait jamais de remplacement
  pour les tuiles « 010 »/« 011 » (statut sans proposition). Corrigé
  (`ocr_title` dans toutes les evidences, version OCR 1.5.0 pour invalider les
  caches) et décision de nom serveur avec critère « trouvable dans le
  catalogue » et rapprochement canonique du titre OCR.
- La recherche FTS joint les mots par `AND` en préfixes : une requête de nom
  brut (« U Kalitas, traitre des Get | r », « \ Serra Ascendant ») ne
  renvoyait rien à cause des jetons d'un caractère et de la ponctuation.
  Les requêtes de nommage sont assainies (alphanumériques ≥ 2 caractères) et
  le candidat lui-même est nettoyé de la même façon avant l'enregistrement.

## Périmètre et documentation

- Le catalogue V2 exporte ses éditions sous la version désignée par
  `_bibliotheque_mtg/work/v3-staging/current.json`. Les images prêtes sont aussi
  classées sous `_bibliotheque_mtg/Images/`; `Cartes Magic/` contient uniquement
  les fichiers historiques restant à identifier et leurs métadonnées.
- `config.json` cite `mtgjson`, `gatherer`, `smfcorp` et `magiccorporation` dans
  les priorités mais les marque désactivés. `image_download.py` n’autorise en
  production que `cards.scryfall.io`. Aucun manifest de service actif n’a été créé
  pour ces pistes.
- Les compteurs de couverture changent après chaque import : l'API `/api/v2/stats`
  et le pointeur actif sont les seules références pour l'état courant. Les
  rapprochements historiques sans preuve suffisante restent non attribués.

## Instructions et état du dépôt

- À la vérification du 21 octobre 2026, `AGENTS.md` existe à la racine et prescrit
  la lecture de `ai-manifest/index.md`, puis des seuls manifests concernés avant
  toute modification du code. Aucun code applicatif n’a été modifié pendant cette
  passe documentaire.
- `ai-manifest/` existait déjà au début de cette passe et contenait les documents
  listés dans son index. Plusieurs déclarations d’absence dans les anciens textes
  étaient donc périmées ; elles ont été corrigées. La vérification locale a trouvé
  23 fichiers Markdown et 153 liens relatifs sans destination manquante.
- La racine est un dépôt Git (`git rev-parse --show-toplevel` la confirme). Le
  `.gitignore` excluait initialement `ai-manifest/`, ce qui empêchait le suivi de
  cette documentation ; cette exclusion a été levée pour que les manifests
  puissent accompagner le programme. Les données de cartes et rapports générés
  restent ignorés.
- `git status` peut montrer `AGENTS.md` comme non suivi dans cet environnement ; ce
  fichier est présent localement et a été lu. Ne pas le remplacer ni en déduire
  qu’il manque.
- `validation_v2.md` décrit un contrôle du 21 octobre 2026 et ses 847 WebP ne
  représentent pas les images ensuite importées. Le Python applicatif observé est
  3.11.8 ; la suite V2 a été relancée avec le Python disponible dans le poste en
  définissant `PYTHONPATH=tests_v2`.
- Un ancien aperçu `Mettre_a_jour_bibliotheque.bat --dry-run` portait sur
  `v2-2b75e3240b3e`. Ne pas l'utiliser pour identifier la version actuelle : seul
  `current.json` fait foi.

## Questions ouvertes

1. Faut-il exposer les exports V2 dans `Cartes Magic/` pour d’autres outils, ou
   garder ce dossier comme archive et lire `current.json` ?
2. Quel comportement de relance veut-on après trois GET infructueux pour une face
   et une langue ?
3. Quel mécanisme doit figer les images pendant une sauvegarde cohérente si
   l’application reste ouverte ?

Voir les [risques structurels](dependencies.risks.md) pour les dépendances et les
contrôles à maintenir.
