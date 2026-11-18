# Module — images à la demande

## Responsabilité

Pour une impression et une face existantes, chercher une image française puis
anglaise puis japonaise puis phyrexiane dans les URL Scryfall de ses enregistrements source, valider le fichier,
le convertir en WebP et publier son état local. Aucune image blanche n’est créée
pour un échec.
Si la ligne française Scryfall manque, le téléchargement normal peut utiliser
une URL Wizards officielle liée à l'ID exact dans la base locale ou une entrée
de galerie produit dont le code d'édition, le nom français et le numéro de
collection concordent. Une correspondance fondée seulement sur le libellé HTML
reste réservée à la prévisualisation avec choix humain.

## Emplacement et état

- `image_download.py` contient `download_printing`, la sélection d’URL, `_fetch`,
  la conversion et `_publish_face`.
- `image_sources_manual.json` contient les exceptions alternatives revues
  manuellement; chaque entrée est exacte par code d’édition, numéro de collection
  et face, avec URL, langue, source et preuve.
- `local_app.py` déclenche l’opération par `POST /api/v2/download` et permet
  l’annulation.
- Le journal durable est `_bibliotheque_mtg/state/v2-image-download-ledger.json` ;
  les assets sont dans la version active sous `assets/webp/<langue>/`.
- Les mêmes images prêtes sont présentées de façon permanente sous
  `_bibliotheque_mtg/Images/<édition>/`, par impression et face, avec un lien
  physique lorsque le volume le permet. Ce miroir est aussi reconstruit au
  démarrage depuis la base active pour récupérer une interruption. La lecture
  des slots prêts et de leurs métadonnées est groupée en une requête avant
  vérification des fichiers. Les noms
  comprennent le code et le nom d'édition, le numéro de collection, le nom de la
  carte, la face et un suffixe dérivé de l'identité d'impression.
- `tests_v2/test_download_v2.py` couvre langues, faces, budget, erreurs réseau,
  reprise et réparation.
- Le téléchargement groupé peut chevaucher au plus quatre GET d'images. Les
  départs vers `cards.scryfall.io` sont espacés d'au moins 250 ms, et un 429
  reporte globalement les suivants selon `Retry-After`. Le journal et la
  publication restent sérialisés ; les appels de téléchargement individuels
  conservent leur comportement ordinaire. Un import manuel reste autorisé
  pendant ce lot ; avant de publier une image récupérée, le lot revérifie le
  slot pour conserver une image publiée entre-temps.

Les anciens algorithmes de `images.py` servent au
[parcours historique](historique.manifest.md) ; le bouton de l’application V2
appelle `image_download.py`.

## Interface et sélection

1. `download_printing(provider_id, catalogue_root, ledger_path, face_indexes, repair, ...)`
   résout l’ID fournisseur en `printing_id` et exige une édition éligible.
2. Le module charge les lignes source FR, EN, JA et PH de la même impression, puis les
   `image_uris` de la face demandée.
3. Il tente FR avant EN puis JA, avec au plus trois GET par langue et par face. Les deux
   dimensions source doivent dépasser 400 px et l’image ne doit pas être presque
   unicolore. Le premier candidat français conforme est préféré ; sinon le premier
   candidat anglais conforme est retenu, puis japonais ou phyrexian si nécessaire.
4. `_fetch` autorise en production seulement `https://cards.scryfall.io`, interdit
   les redirections, exige un type image et borne la réponse à 20 MiB.
5. Pillow vérifie le décodage et les dimensions, applique l’orientation EXIF,
   réduit l’image avec Lanczos pour la faire tenir sans rognage ni étirement dans
   un canevas transparent de 531×738, puis encode en WebP qualité 95. Chaque
   fichier enregistré mesure exactement 531×738 ; la provenance distingue les
   dimensions source et sortie.
6. `_publish_face` remplace le fichier de contenu, actualise `images.json`, puis
   met à jour `image_assets` et `image_slots` dans SQLite.

### Relance approfondie ciblée

- `refresh_printing_deep(provider_id, ..., face_indexes=...)` est une opération
  backend explicite pour rechercher une meilleure image d’une impression précise.
  L’ID fournisseur sert uniquement à résoudre son `printing_id`; les lignes FR/EN,
  slots, manifeste d’édition et mise à jour restent liés à ce même `printing_id`,
  `set_id` et index de face. Aucun rapprochement par nom n’est permis.
- La relance ignore l’état `ready` et le plafond historique déjà consommé du
  ledger pour cette opération ciblée, sans effacer le ledger historique. Les
  requêtes restent bornées et toute tentative est enregistrée sous une clé
  distincte de relance approfondie.
- Le mode de prévisualisation applique les mêmes recherches et contrôles de
  source mais ne publie pas le résultat. Il examine jusqu'à six URL par langue
  FR et EN, déduplique les octets identiques et écarte l'URL et le hash source
  de l'image déjà publiée. Il rend plusieurs images sources validées avec leur
  langue et leurs dimensions d'origine. La validation d'une variante choisie
  revérifie la version active, l'impression, la face et l'asset antérieur avant
  d'appeler la publication existante. Une candidate refusée ou expirée ne
  modifie pas `image_slots`, `image_assets` ni `images.json`.
- Si aucune nouvelle image n'est trouvée, le parcours IA peut utiliser l'image
  prête du slot exact comme référence visuelle, après validation du chemin et
  du décodage. Elle ne devient pas une nouvelle variante à valider dans le
  comparateur ordinaire.
- La prévisualisation consulte aussi la galerie officielle indexée de l'édition
  et y vérifie le nom français exact. Si l'impression n'a pas de ligne FR, elle
  retrouve ce nom parmi les autres impressions du même oracle. Elle teste
  l'URL Lotus Noir fondée sur le code d'édition et le numéro exact. Ces sources
  sont limitées à leurs hôtes HTTPS et réservées au choix manuel. La recherche
  avancée accepte toute taille d'image décodable et montre ses dimensions
  originales avant validation ; le téléchargement normal garde son seuil.
- La recherche approfondie examine les sources FR puis EN sans s'arrêter à la
  première image valide ; l'utilisateur compare les variantes et valide son choix.
  La qualité est évaluée sur les dimensions source ; les URL Scryfall de qualité
  supérieure sont tentées en premier. La provenance du nouvel asset indique la
  source, la langue, les dimensions et le mode approfondi.
- Chaque relance approfondie fait au plus un GET de métadonnées sur
  `https://api.scryfall.com/cards/<set>/<collector_number>/fr`, après un intervalle
  minimal de 600 ms entre ces appels. La réponse FR live est vérifiée par code,
  UUID de série, numéro exact, langue, layout, forme des faces et preuve d’identité
  commune (`oracle_id` ou `illustration_id`) avant d’ajouter ses URLs d’image aux
  candidats. 404, 429, refus ou mismatch d’identité n’autorisent pas cette source;
  le snapshot et les entrées manuelles restent utilisables selon leur contrat.
- Si une URL de la réponse live fournit l’image retenue, la provenance conserve
  `provider=scryfall_api` et `source_record_id` (ID de la ligne FR Scryfall); les
  lignes source du catalogue restent inchangées.
- Cette requête ne modifie pas le catalogue ni ses lignes source; elle ne sert qu’à
  rafraîchir l’image du slot de l’impression exacte. L’API Scryfall est utilisée
  uniquement en HTTPS, sans redirection, avec réponse JSON bornée et User-Agent.
  Un 404 est mis en cache une heure; un 429 reporte la requête selon
  `Retry-After`, et un 403 bloque les requêtes live futures pour cette impression.
- Un échec ne doit pas remplacer un asset prêt valide. Un succès publie
  atomiquement le nouveau contenu et conserve les invariants de cohérence entre
  fichier, SQLite et `images.json`. Si toutes les sources échouent alors qu’un
  slot était prêt, le résultat est `refresh_failed_kept_existing` et le slot prêt
  reste intact.
- Les seuls hôtes alternatifs permis proviennent de l’entrée correspondante du
  registre (`www.lotusnoir.info` ou `media.wizards.com` pour les quatre cas WHO).
  L’autorisation est limitée à l’URL de cette entrée et au mode approfondi; les
  règles Scryfall restent inchangées pour les autres images.
- `split`, `flip` et `adventure` peuvent avoir une image physique au niveau racine
  même si `card_faces` contient plusieurs faces textuelles. Le sélecteur utilise
  les URL de face quand elles existent, puis retombe sur `image_uris` racine.
- Les deux scans Lotus Noir WHO/77 et WHO/131 (299×433) sont explicitement admis
  comme exception locale au seuil standard de 401 px, uniquement si le slot n’a
  pas déjà un asset prêt de meilleure qualité. Pour un asset prêt, un candidat de
  moindre aire source produit `lower_quality_kept_existing`.

## Invariants et échecs

- L'import explicite d'un fichier local accepte une image décodable de 20 MiB
  maximum, conserve l'identité exacte d'impression et de face, puis réutilise
  la conversion 531×738 et la publication par hash. Sa provenance indique
  `manual_import` sans conserver le chemin privé du fichier source.
- La reprise d'une archive historique utilise la même publication, avec la
  langue FR/EN identifiée, un SHA-256 source contrôlé et une provenance
  `historical_import`. Elle ne remplace pas un slot déjà prêt. L'encodage WebP
  historique peut utiliser la méthode 4 à qualité 95 pour traiter le lot local
  plus vite ; les dimensions et contrôles de publication restent identiques.
- Lorsqu'un fichier de `Cartes Magic/Inconnu/` réunit une preuve unique de code,
  numéro de collection et titre, il suit cette même publication locale. Une image
  prête déjà existante est conservée ; l'original n'est supprimé qu'après contrôle
  de l'image organisée correspondante.

- La carte, l’impression, l’édition et l’index de face doivent correspondre aux
  lignes source ; aucun choix par nom seul.
- Un slot `ready` déjà valide est réutilisé sans GET. Une copie signalée corrompue
  exige une réparation explicite.
- Un ancien WebP valide mais non normalisé est converti localement à 531×738 lors
  de la prochaine action de téléchargement/réparation sur cette impression.
- 403 bloque la source, 429 différera selon `Retry-After`; HTML, contenu corrompu,
  redirection et URL hors liste sont rejetés.
- Le ledger persiste les GET et erreurs pour permettre la reprise. La politique
  après épuisement des trois GET mérite une décision explicite, voir
  [audit](../audit.md).
- Un échec local de publication SQLite après une image valide ne doit pas
  épuiser définitivement le budget réseau de cette image ; le téléchargement
  peut reprendre une fois le verrou résolu.
- La vérification d’un ancien `asset_<lang>_<sha256>` repris peut actuellement
  produire un faux diagnostic de corruption ; cet écart est consigné dans
  [l’audit](../audit.md).

## Dépendances et points à vérifier

- [Catalogue](catalogue.manifest.md) pour les IDs et `raw_json`,
  [Scryfall](../infra/scryfall.manifest.md) pour les URL d’image,
  [stockage](../infra/stockage-local.manifest.md) pour la base et les fichiers.
- Tester l’image FR de bonne qualité, le repli EN, la résolution insuffisante, les
  deux faces, un échec fournisseur et la réparation d’un asset déjà présent.
- Après publication, comparer le hash du fichier, la langue et le chemin du slot
  dans SQLite et `images.json`.
