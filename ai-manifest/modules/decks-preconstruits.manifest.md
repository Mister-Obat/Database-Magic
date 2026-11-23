# Module — decks préconstruits

## Responsabilité

Stocker localement des listes de decks préconstruits et exposer une API Python
de consultation/import à l'interface. Ce module reste distinct du catalogue de
cartes V2 et de son cycle de publication.

## Emplacements

- `bibliotheque_mtg/decks.py` : import JSON, base SQLite, recherche et lecture
  d'un deck avec résolution facultative vers le catalogue de cartes.
- `tests_v2/test_decks.py` : tests de parsing, stockage et résolution.
- `bibliotheque_mtg/data/doctor_who_planechase.json` : complément local des dix
  cartes Planar Deck de Timey-Wimey, avec les numéros WHO et IDs Scryfall quand
  présents dans l'index de travail de l'utilisateur ; vérifié contre la liste
  officielle Wizards.
- La base de decks par défaut est
  `_bibliotheque_mtg/state/preconstructed-decks.sqlite` ; elle n'est pas intégrée
  à la base V2 des cartes.

## Format et API

- L'import accepte MTGJSON `DeckList` (`data` contenant les decks) et le JSON
  communautaire `decks_v2.json` de taw (`cards`, `commander`, `sideboard`,
  `displayCommander`, `planarDeck`, `schemeDeck`). Les champs inconnus sont
  conservés dans le JSON source de chaque deck. L'import de ce JSON
  utilise uniquement le contenu fourni.
- `ensure_deck_index(db_path)` initialise l’index local absent depuis le fichier
  MTGJSON `DeckList.json` (environ 656 Ko au 15 novembre 2026) ;
  `refresh_deck_index(db_path)` le met à jour explicitement. Le bouton de mise à
  jour de la page Info appelle cette actualisation après celle du catalogue :
  les nouveaux produits deviennent consultables et rejoignent la file de
  chargement progressif. `refresh_deck_index` accepte un callback de
  progression couvrant le téléchargement puis l’import de l’index.
- `import_decks(source_json, db_path)` importe un index ou une liste de decks
  transactionnellement et retourne les compteurs.
- Les noms français sont exposés depuis les traductions explicites fournies à
  l'import (`name_fr`/`nameFr` ou `translations.French` pour le deck,
  `set_name_fr`/`setNameFr` ou `setTranslations.French` pour l'édition), ainsi que
  depuis les alias locaux vérifiés contre les pages françaises officielles
  Wizards. À défaut, ils restent `null` et l'interface peut afficher le nom
  anglais seul. Aucune traduction automatique ni déduction à partir des cartes
  n'est permise. Les quatre alias WHO vérifiés sont `Voyage dans le passé`,
  `Méli-mélo spatio-temporel`, `Puissance paradoxale` et `Maîtres du mal`; le nom
  français de l'édition WHO demeure `Doctor Who`.
- `list_decks(db_path, query=None, deck_type=None, deck_format=None, colors=None,
  set_code=None, sort='date', order='desc', limit=50, offset=0)` renvoie un objet
  paginé avec `total`, `limit`, `offset` et `items`. Le type de produit est conservé ; le
  format est une valeur dérivée explicitement (`Commander`, `Standard`,
  `Pioneer`, `Modern`, `Autre`). La recherche inclut les titres français et les
  noms français d'édition quand ils sont fournis ; `set_code` filtre une édition
  par son code stable. Chaque item conserve `name`/`set_name` en anglais et ajoute
  `name_fr`/`set_name_fr` (null si indisponible).
- L'option **Tous les decks** inclut les decks avec et sans couverture.
- La recherche peut filtrer les decks dont l'image de couverture est prête ou
  absente. Ce filtre s'applique avant pagination ; les decks sans composition
  chargée sont considérés sans image. Le résultat de cette résolution locale est
  mis en cache par version catalogue et empreintes de bases, puis invalidé après
  une mise à jour de l'index ou une modification des slots d'image. Le tri offre
  la date de sortie MTGJSON ou la date de première entrée dans l'index local,
  chaque critère avec un sens ascendant ou descendant. Lors de la migration, les
  anciens decks utilisent la date de sortie comme approximation historique,
  avec un horodatage de référence si elle manque ; les nouveaux decks reçoivent
  leur date réelle d'entrée locale dans `added_at`, que les actualisations
  suivantes préservent.
- `deck_facets(db_path)` expose types/formats disponibles, éditions distinctes
  (`editions`: objets `{code, name_fr, name_en}`) et couverture des couleurs
  (`color_indexed`, `total`) pour que l'interface ne masque pas les compositions
  encore non chargées.
- `POST /api/v2/decks/recalculate` reçoit les filtres actifs de la recherche
  (`q`, `deck_format`, `deck_type`, `set_code`, `colors`, `color_mode`, `sort`,
  `order`) et recalcule hors réseau la résolution des cartes et l'état des images
  pour tous les decks correspondants dont la composition et les jetons sont déjà
  chargés. Il ne télécharge ni composition absente ni image et retourne seulement
  `{total, recalculated, unloaded}`. Après cette action, `GET /api/v2/decks` fournit
  sur chaque item de la page `cover_image_path`, `media_summary` (`total`, `ready`,
  `missing`) et `complete` ; `complete` vaut `null` si la composition ou les jetons
  ne sont pas chargés, sinon il indique si toutes les impressions uniques ont
  toutes leurs faces physiques prêtes.
- `index_deck_colors(db_path, catalog_db, progress_callback=None)` complète en
  tâche de fond les couleurs absentes de l'index courant. Il récupère le relevé
  compact des préconstruits MTGJSON via Hugging Face `datasets-server`, joint
  ses `oracle_id` aux identités de couleur du catalogue V2 actif, puis ne met à
  jour que les decks correspondants. La progression inclut l'avancement, les
  identités non résolues et les octets reçus. Les compositions, impressions et
  autres métadonnées des decks ne sont pas remplacées.
- Les listes sélectionnables sont limitées aux valeurs de type reconnues comme
  produits deck (Commander, Challenger, Theme, Intro, Jumpstart, Duel, Starter,
  Arena Promotional Deck, Dandan Deck et familles voisines). Les produits
  MTGJSON comme boosters, paquets Booster Battle Pack (10 cartes par paquet,
  insuffisant pour constituer un deck), bundles, drops Secret Lair et
  redemptions MTGO restent dans la source mais ne polluent pas l'onglet des
  decks. Les catégories mixtes comme Box Set ne sont pas incluses par défaut.
- `get_deck(db_path, deck_id, catalog_db=None, fetch_missing=True)` renvoie les métadonnées et les
  sections `mainboard`, `sideboard`, `commander`, `display_commander`,
  `planar_deck`, `scheme_deck`, `tokens`. La section `tokens` vient des jetons
  explicitement fournis par la composition MTGJSON du deck. Si cette liste est
  vide, les relations de jetons documentées dans les enregistrements des cartes
  du deck peuvent compléter la section, mais seulement si une impression de
  jeton de l'édition de jetons associée (`T` + code du deck) est identifiée sans
  ambiguïté. Un lien `all_parts` pointant vers une autre édition ne justifie pas
  l'affichage de son image dans ce deck. Les anciens jetons dérivés d'une autre
  édition sont retirés du cache du deck à sa prochaine lecture. La section s'affiche
  après les autres sections. Chaque ligne expose nom, quantité, édition,
  numéro de collection, UUID MTGJSON, foil et `provider_card_id` résolu ou null.
  Quand la même impression possède un enregistrement français dans le catalogue,
  `printed_name` porte ce nom français ; l'impression doit être prouvée avant
  d'utiliser cette traduction.
  Les métadonnées du deck conservent `name`/`set_name` anglais et exposent aussi
  `name_fr`/`set_name_fr` selon la règle de traduction explicite ci-dessus.
- `get_deck` peut télécharger uniquement le fichier deck sélectionné depuis
  `https://mtgjson.com/api/v5/decks/<fileName>.json`, puis le mettre en cache ;
  `fetch_missing=False` garde l'appel strictement hors ligne.
- Après l'ouverture de l'application, un seul travailleur parcourt l'index
  local des préconstruits. Il traite une composition manquante à la fois en
  utilisant le fichier individuel MTGJSON `.json.xz`. Le premier passage est
  différé de 60 secondes et deux tentatives sont espacées d'au moins 120
  secondes. Les échecs sont reportés par deck dans SQLite pour laisser les
  autres passer et reprendre après un redémarrage. Un deck en échec est
  réessayé après au moins une heure. Une erreur réseau, un refus de la source
  ou une limitation de débit met le travailleur en pause au moins 15 minutes,
  avec respect de `Retry-After`. Le fichier compressé est borné à 1 Mio et le
  JSON à 8 Mio. Les jetons sont dérivés du catalogue local selon les règles
  de résolution existantes. Si l'index est absent ou si la file atteint la
  fin, le travailleur sollicite `DeckList.json.xz` avec les mêmes bornes.
  Il enregistre la prochaine tentative avant le transfert, avec au moins
  sept jours entre deux tentatives de rafraîchissement automatique, même
  après un échec ou un redémarrage. L'import préserve les compositions
  déjà chargées et ajoute les nouveaux decks à la file.
  Les téléchargements manuels, l’indexation des couleurs et la maintenance
  ont priorité sur les nouvelles requêtes du travailleur.
- Les identifiants sont reproductibles : `mtgjson:<fileName>` si disponible,
  sinon code d'édition + nom normalisé, avec empreinte déterministe pour
  départager les collisions.

## Invariants

- L'import valide toutes les entrées avant de remplacer l'instantané SQLite ; une
  erreur ne doit pas laisser une base partielle.
- La navigation par métadonnées utilise l'index local. L'interface peut lancer
  une seule indexation distante des couleurs en tâche de fond si la couverture
  est incomplète ; l'indexation ne remplace pas les métadonnées locales.
  Le chargement de fond de compositions n'effectue aucun téléchargement
  d'image, de catalogue d'éditions ou d'archive de decks.
- Les couleurs sont null tant que la composition n'est pas chargée ; après
  chargement, elles sont calculées depuis les identités de couleur des cartes
  jouables, hors jetons, cartes de plan et machinations. Un jeton dérivé ne
  modifie jamais l'identité de couleur indexée du deck.
  Un filtre couleur ne renvoie que les decks indexés couleur.
  Les filtres d'édition, de format et de type de produit acceptent plusieurs
  valeurs ; une sélection vide explicite renvoie zéro deck.
- Le filtre accepte W/U/B/R/G et C ; `C` ajoute les decks
  indexés incolores (`colors=''`) aux résultats des couleurs choisies.
  En mode `contains` (défaut), les autres couleurs cochées doivent toutes être
  présentes et le deck peut en contenir d'autres. En mode `exact`, l'identité
  de couleur indexée doit correspondre exactement à l'ensemble choisi. Sans
  couleur choisie, aucun mode ne filtre les couleurs.
- Les quantités restent strictement positives et les lignes de section ne sont
  pas fusionnées entre sections.
- La résolution utilise `identifiers.scryfallId` du deck s'il est présent, puis le
  couple édition + numéro de collection ; elle ne réussit que si l'identifiant
  désigne une seule impression de l'édition déclarée sur la ligne du deck, dans
  une édition éligible du catalogue actif. Le téléchargement, la vignette et
  l'export vérifient également cette édition avant d'utiliser l'image.
  Le nom seul ne suffit jamais.
- Le catalogue de cartes est ouvert en lecture seule et n'est jamais modifié.
- Une carte non résolue conserve ses données de liste et renvoie
  `provider_card_id: null`.
- Avant un téléchargement, la composition expose le nombre d'impressions
  résolues dans le catalogue actif. Si aucune ne l'est, le lancement est refusé
  avec un message d'actualisation du catalogue, sans créer une tâche de 100 %
  d'échecs. Les impressions absentes d'un deck partiellement résolu sont
  comptées séparément comme indisponibles ; seuls les échecs de téléchargement
  d'impressions identifiées alimentent le compteur d'échecs.
- Le téléchargement de deck vérifie chaque impression et chaque face dans la
  version active, puis ne traite que les slots absents, y compris ceux des
  jetons. Un deck entièrement prêt propose l'export local sur le Bureau à la
  place du bouton de téléchargement. L'export contrôle à nouveau la complétude
  avant de créer un dossier nouveau sans remplacer un export antérieur.
- La vignette d'un deck reprend la première carte de la première section non
  vide affichée dans sa fiche. L’API place Commandants en tête, puis
  Commandants de présentation et Jetons à la fin, dans cet ordre. Les autres
  sections et les cartes de chaque section conservent leur ordre.
  Une composition réduite à la section Jetons, comme Enemy Deck (TMT), utilise
  sa première carte. La première carte garde cet emplacement
  même quand son image manque. Une identité inconnue reste un emplacement vide ;
  aucun choix par nom du deck ou par image d'une autre édition n'est permis.
- Au retour de la composition, si la couverture renvoyée par la fiche diffère
  de celle de la liste en mémoire, les résultats sont relus avec les filtres
  actifs. Cela inclut une composition chargée depuis l'ouverture de la fiche.
- Le rafraîchissement ciblé de la recherche active réévalue tous les résultats
  correspondant aux filtres, pas seulement la page affichée. Cette lecture ne
  modifie pas l'index ni les compositions en cache ; les identités et images sont
  résolues depuis le catalogue local courant.

## Sources et approche

Le modèle des decks MTGJSON documente code, nom, date, type, `mainBoard`,
`sideBoard`, `commander`, tokens et UUID de produits scellés. Son index de
métadonnées comprend toutes les listes préconstruites et chaque composition
est disponible dans un fichier individuel. Le dépôt
`taw/magic-preconstructed-decks-data` fournit aussi un format JSON avec des
sections supplémentaires et avertit que les impressions exactes de certaines
cartes peuvent rester heuristiques. L'import local ne télécharge pas ces données
et ne traite pas les noms comme preuve d'identité.

Le catalogue V2 local fournit les noms anglais complets des éditions. Le fichier
`bibliotheque_mtg/data/set_names_fr.json` extrait les valeurs `translations.French`
du `SetList.json.gz` de MTGJSON : 146 codes explicitement traduits au moment de
l'extraction. Une édition sans traduction documentée reste en anglais. Les lignes
du deck existant sont enrichies lors de la migration du schéma, et la recherche
utilise les noms français stockés sans distinction d'accents.

Les traductions WHO sont les intitulés éditoriaux de l'article français officiel
des listes de decks :
https://magic.wizards.com/fr/news/announcements/magic-the-gathering-doctor-who-commander-decklists.

Références : [MTGJSON Deck Data Model](https://www.mtgjson.com/data-models/deck/),
[types Scryfall all_parts](https://github.com/scryfall/api-types/blob/main/src/objects/Card/CardFields.ts),
[MTGJSON Translations Data Model](https://www.mtgjson.com/data-models/translations/),
[données communautaires préconstruites](https://github.com/taw/magic-preconstructed-decks-data),
[sources et validation communautaires](https://github.com/taw/magic-preconstructed-decks),
[index compact des listes MTGJSON](https://huggingface.co/datasets/kasper-cbx/mtg-search-index).
Ce dernier projet republie les listes depuis MTGJSON (MIT) dans un jeu de données
global étiqueté `derived-scryfall-wotc-fan-content`; l'application n'en conserve
que la couleur calculée localement, jamais les listes ni les identifiants de
cartes de cette source.
