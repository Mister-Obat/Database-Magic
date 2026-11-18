# Module — interface et recherche locales

## Responsabilité

Présenter le catalogue publié sans dépendance distante pendant la consultation :
accueil, statistiques, éditions, decks préconstruits, recherche filtrée, fiche
exacte, images présentes et archive historique. Le serveur transmet une demande
explicite au module image.

## Emplacements

Pour les changements visuels, suivre la [direction artistique de l’interface](direction-artistique.manifest.md).

- `local_app.py` : serveur HTTP, routes, statistiques, enrichissement des slots,
  service des fichiers et POST de téléchargement.
- La cloche globale rassemble les traitements actifs et les candidates à revoir.
  Cliquer sur un téléchargement groupé rouvre la vue et les filtres ayant lancé
  la tâche (édition, recherche ou decks), puis son suivi. Cliquer sur un
  téléchargement de deck rouvre ce deck et sa progression, y compris depuis
  une autre fenêtre. Une notification de maintenance rouvre Info et son suivi.
  Les filtres du lot sont conservés dans sa tâche serveur.
  Une pastille rouge à cheval sur l'angle inférieur droit du bouton signale au
  moins une tâche `needs_review` non encore consultée. L'ouverture du panneau
  la masque pour les tâches présentes, y compris après rechargement ; une nouvelle
  tâche à vérifier la fait réapparaître avec un son discret. Plusieurs tâches
  arrivant pendant que la pastille reste visible ne rejouent pas le son. La revue IA rouverte depuis
  une autre fenêtre reprend la même structure de panneaux que la revue initiale.
  Une génération IA lancée depuis la fiche reste accessible après fermeture de
  sa modale : sa ligne rouvre la progression, son éventuelle erreur ou la revue
  des trois cartes. L'annulation d'une génération active est une action explicite
  distincte de la fermeture de la modale.
  Le bouton de retour pendant l'attente ferme les modales de traitement et de
  carte sans changer l'onglet, le fragment d'URL ni la position de consultation.
  Pendant l'attente, l'annulation est placée à gauche du retour à la consultation,
  sur une même ligne avec un espacement distinct.
- `list_import.py` : analyse une liste collée et résout les noms FR/EN et URL
  EDHREC contre les identifiants exacts du catalogue actif.
- `list_export.py` : exporte le résultat de la dernière liste traitée dans un
  dossier unique de PNG sur le Bureau, avec emplacements blancs pour les images
  absentes.
- `v2/api.py` : aiguillage des routes catalogue ; `v2/query.py` : requêtes SQLite
  en lecture seule et recherche FTS5.
- Les lignes de langue phyrexiane (`ph`) conservent leur texte imprimé brut dans
  `raw_json` ; leurs champs `printed_*` de consultation sont vides. Les réponses
  emploient le `name` canonique : la transcription des glyphes phyrexians n'est
  pas un titre lisible.
- `ui/index.html`, `ui/app.js`, `ui/styles.css` : affichage navigateur et appels à
  l’API.
- Une nouvelle soumission de Recherche ou de Decks annule côté navigateur la
  requête de liste encore en cours avant de lancer la suivante ; ouvrir une autre
  fiche annule de même le chargement précédent. Les réponses obsolètes restent
  ignorées. La fermeture de la connexion n’interrompt pas nécessairement un
  calcul SQLite déjà démarré côté serveur.
- Le choix d'image existante et les deux étapes de références IA partagent la
  même position de vignettes et d'aperçu. Les consignes des références sont à
  droite du titre, hors de la sélection. Les images comparées restent visibles
  en entier dans la hauteur du dialogue. Les actions de revue IA et de la
  confirmation de traduction sont centrées ; Annuler pendant une recherche
  d'image ne présente pas d'infobulle au survol.
- Aux étapes 1 et 2 de sélection des références IA, les boutons d'action
  restent au bas du panneau de vignettes, comme pour le choix d'image existante.
- `ui/selects.js` conserve chaque `<select>` comme source de vérité pour les
  formulaires et les événements `change`, mais remplace son menu natif visible
  par une liste sombre et animée. Cela évite le flash gris du menu système dans
  Firefox, y compris pour les sélecteurs ajoutés dynamiquement.
- Le logo seul identifie le lien Accueil dans l’en-tête. Le logo et le favicon utilisent `Logo.png` et `Logo.ico` à la
  racine du projet, servis par les routes locales `/logo.png` et `/favicon.ico`.
- Le démarrage ne synchronise pas intégralement les images organisées. Cette
  opération parcourt tout le catalogue et doit rester hors du chemin critique ;
  chaque ajout ou réparation met à jour le miroir au fil de l'eau.
- Tests : `tests_v2/test_local_app.py` et `tests_v2/test_query_v2.py`.

Le lanceur utilisateur est `Lancer_Bibliotheque_Magic.bat`. Voir la
[carte des fichiers](../modules.map.md).

## Interfaces principales

- `/1` et `/2` servent le même `index.html`, `app.js` et `styles.css` que
  l'application. Ils ouvrent automatiquement le comparateur existant avec une
  paire d'images locales de deux impressions du même oracle. `/1` montre le
  choix d'image existante ; `/2` montre la revue IA avec une candidate PNG déjà
  présente localement. Ces pages sont des simulations : leurs boutons de choix
  ferment la revue sans publier d'asset. `GET /api/v2/demo-images` sélectionne
  la paire en lecture seule ; `GET /api/v2/demo-ai-image` sert un exemple local.
  Le mode simulation s'applique seulement à la fiche ouverte automatiquement ;
  fermer sa comparaison désactive ce mode et ramène l'URL à `/`, afin qu'une
  recherche d'image ultérieure utilise l'API réelle.

- Les suggestions de recherches récentes se ferment lorsque le champ principal
  perd le focus, sauf pendant la navigation clavier dans ces suggestions. Elles
  se ferment aussi lorsque la fenêtre ou l’onglet perd le focus. Un rappel
  différé ne les rouvre pas sans focus sur le champ ou la liste. L'historique
  n'enregistre un terme qu'au clic sur Rechercher, jamais pendant la saisie ou
  à la fermeture de la page. Le terme déjà saisi n'est pas proposé ; chaque
  suggestion peut être supprimée individuellement.
- La page Info accepte jusqu'à 100 cartes, une par ligne (nom FR/EN, URL HTTPS
  `edhrec.com/cards/<slug>` ou lien Markdown vers cette URL). `POST
  /api/v2/list-import` démarre une tâche locale ; `GET` retourne sa progression et
  le résultat de chaque entrée. L'URL sert uniquement à extraire le nom : aucune
  page EDHREC n'est téléchargée. Le nom exact est retrouvé via FTS5 et vérifié
  après normalisation des accents ; sans édition explicite, l'impression éligible
  la plus récente est choisie, avec préférence pour la ligne FR de cette
  impression. Les lignes sans identité exacte sont signalées comme introuvables.
  Chaque impression résolue passe par `download_printing` et rejoint les mêmes
  slots, WebP, manifestes et dossiers d'édition que la fiche. La mise à jour du
  catalogue est bloquée pendant cet import.
  Le suivi affiche les mêmes tuiles d'image cliquables que Recherche et Éditions ;
  la visionneuse permet de parcourir les impressions résolues de la liste.
  `POST /api/v2/list-import/export` requiert une tâche terminée et crée un dossier
  `Export cartes Magic` sur le Bureau (suffixe numérique si déjà présent).
  Chaque face est un PNG nommé `<valeur de mana> - <nom français>.png` quand la
  traduction existe (sinon nom source), avec
  suffixe de face et de doublon si nécessaire. La source est relue par identité
  d'impression et contrôlée avant export ; une image absente devient un PNG blanc
  de 531×738. Une entrée introuvable garde le nom saisi et un coût `0` parce que
  le catalogue ne fournit aucune métadonnée vérifiée pour elle. Aucune image
  blanche n'est ajoutée au catalogue lui-même.

- `GET /api/v2/health`, `/stats`, `/editions`, `/search`, `/edition`, `/card` pour
  le catalogue.
- La liste `/edition` conserve chaque impression et son numéro de collection
  exact. Seules les langues de cette même impression sont réduites à une ligne
  préférée (FR, EN, JA, puis PH et autres langues disponibles). Deux variantes
  d'une même carte dans la même édition restent donc toutes deux consultables.
- `POST /api/v2/search/image-slots` accepte de 1 à 200 `printing_ids` et renvoie
  les slots locaux de ces impressions afin de rafraîchir les tuiles de la page
  Recherche sans recalculer ses résultats.
- Le filtre « Jetons » de la recherche utilise `editions.set_type='token'`.
  Cette catégorie comprend les cartes d'aide des éditions de jetons, notamment
  la carte de substitution recto-verso SMH3/1. Elle provient du catalogue
  Scryfall et subsiste donc lors des actualisations sans entrée locale ajoutée.
- Les éditions de jetons du snapshot actif n'ont aucune ligne source française.
  La recherche déduit à la demande les équivalences de sous-types français et
  anglais à partir des lignes bilingues de même impression et les ajoute au
  terme FTS d'origine. Les noms de jetons utilitaires qui ne figurent pas comme
  sous-types sont couverts par une petite liste explicite (`indice` vers `clue`,
  par exemple). Les résultats sont mis en cache par terme et invalidés au
  changement de base. Cela couvre les termes attestés dans le corpus bilingue,
  sans prétendre traduire les noms propres ou les termes absents de ce corpus ;
  aucun texte ni aucune identité d'impression publiée n'est modifié.
- Le panneau de notifications se ferme au clic hors de la cloche et de son
  contenu ; `aria-expanded` revient alors à `false`.
- Les en-têtes Recherche, Éditions et Decks proposent un bouton icône de
  rafraîchissement qui relit la sélection courante et ses images locales sans
  lancer de téléchargement. Dans Recherche, le bouton reste actif même sans
  résultat et le clic ne fait alors rien. Dans Decks, ce rafraîchissement réévalue tous les
  decks de la recherche active, y compris hors de la page affichée, pour rendre
  visibles les commandants et l'état complet des compositions déjà chargées.
  Dans Recherche, le bouton conserve la page et ses impressions affichées puis
  relit leurs slots locaux via l'endpoint dédié ; il ne relance pas le moteur de
  recherche et ne contacte aucun fournisseur.
  Les en-têtes Recherche, Éditions et Decks exposent un téléchargement explicite
  des images manquantes sur l'ensemble des résultats de la sélection courante,
  au-delà de la page affichée. Le serveur reconstruit la sélection avec les
  filtres appliqués, déduplique les impressions exactes, saute les slots déjà
  prêts et traite les autres avec `download_printing`. La tâche asynchrone expose
  progression, erreurs et annulation ; les mises à jour du catalogue sont
  bloquées pendant son exécution. Un filtre actif est requis pour les Decks.
  Le lot emploie au plus quatre workers pour les téléchargements d'impressions,
  avec au plus quatre départs de requêtes d'images Scryfall par seconde.
  Tant que le total des impressions manquantes n'est pas calculé, le suivi
  affiche une analyse en cours avec un indicateur animé, plutôt que « 0/0 ».
  Dès que ce total est connu, le suivi affiche près des impressions traitées
  un temps restant estimé. Il part de la cadence observée de 11 376 images en
  huit heures, puis se réajuste à la cadence du lot courant. Le décompte est
  indicatif et disparaît lorsque la tâche se termine.
  Dans Decks, le suivi du téléchargement se place à droite des boutons
  Rechercher et Effacer les filtres, sur la même ligne lorsque la largeur le
  permet, comme le statut de Recherche.
- `POST /api/v2/maintenance/update` démarre en tâche de fond l'actualisation du
  catalogue puis de l'index des decks préconstruits ; `GET` du même chemin expose
  l'état (`idle`, `queued`, `running`, `complete`, `failed`), l'étape, le message,
  la durée et les erreurs. La page Info propose le bouton correspondant et
  affiche l'avancement puis l'issue. Un téléchargement d'image ou de deck déjà en
  cours bloque le démarrage ; une actualisation déjà lancée est réutilisée. Une
  fois terminée, l'interface recharge les statistiques, la liste des éditions et,
  si nécessaire, les facettes et la page courante. Dans Recherche, la sélection
  affichée est conservée et seuls ses slots image sont relus ; aucun nouveau
  critère n'est envoyé au moteur de recherche.
- Le suivi expose aussi `steps`, une liste ordonnée (`sources`, `catalogue`,
  `images`, `manifests`, `exports`, `publish`, `decks`) avec `status` (`pending`,
  `running`, `complete`, `failed`, `skipped`), `progress` de 0 à 100 et `detail`.
  Sous l'indicateur de durée, l'interface affiche chaque étape avec son état et
  son pourcentage. Une étape non nécessaire est `skipped` à 100 % ; une étape
  non atteinte reste `pending` à 0 %.
- `POST /api/v2/maintenance/update/cancel` demande l'arrêt de la tâche active
  identifiée par `task_id`. L'état `cancelling` garde le suivi visible jusqu'à
  l'arrêt effectif, puis `cancelled` permet une nouvelle mise à jour. Le bouton
  devient « Annuler la mise à jour » pendant une tâche active.
- `GET /api/v2/maintenance/versions` liste les versions publiées conservées ;
  `POST /api/v2/maintenance/activate` valide puis active une version choisie.
  La page Info propose un historique avec confirmation avant la bascule. Une
  mise à jour ou un téléchargement en cours bloque cette activation.
- `/stats` distingue les slots image prêts/manquants, les éditions dont tous les
  slots sont prêts et les decks locaux déjà chargés dont chaque impression est
  résolue et chaque image présente. Les decks non chargés ne sont pas réputés
  complets. La page Info affiche seulement trois rapports « présents / total » :
  impressions dont toutes les faces ont une image prête sur toutes les
  impressions du catalogue, éditions complètes sur éditions éligibles, decks
  complets sur decks répertoriés dans l'index local. Les trois tuiles occupent
  leur place dès l'ouverture et seuls leurs chiffres sont mis à jour.
- `/health` expose `app_api_revision`. Le lanceur réutilise une instance locale
  compatible et ouvre sa page ; il arrête une instance incompatible seulement si
  sa commande exécute exactement `bibliotheque_mtg.local_app`. Un autre programme
  occupant le port n'est jamais arrêté. Un nouveau serveur est lancé sans fenêtre
  de console ; le BAT attend jusqu'à cinq minutes que `/health` confirme son
  démarrage, y compris la synchronisation préalable des images. La page Info
  signale les compteurs absents au lieu de les afficher à zéro.
- L'en-tête place « Fermer l’application » à gauche des notifications. Les
  commandes ont les mêmes dimensions, et l'en-tête compact utilise des
  pictogrammes avec des libellés accessibles. Son POST
  `/api/v2/app/shutdown` exige une origine locale, répond avant de demander
  `ThreadingHTTPServer.shutdown()` dans un autre thread, puis la page tente de
  fermer l'onglet ; si le navigateur l'interdit, elle est remplacée par une page
  vide. Le clic ouvre d'abord une modale avec Annuler et Confirmer ; seul
  Confirmer envoie le POST. Le libellé « Catalogue local » n'est plus affiché.
- Sur Accueil, « Lancer la mise à jour » est placé à gauche de « Historique des
  versions » sur la même ligne, avec retour à la ligne si l'espace manque.
- `POST /api/v2/image/import` reçoit un fichier image brut (20 MiB maximum) et
  l'identité exacte `provider_card_id`/`face_index` en paramètres d'URL. Il
  normalise et publie dans le slot partagé par toutes les vues.
- `POST /api/v2/image/export` reçoit cette identité exacte en JSON et convertit
  la face locale prête en PNG dans le dossier persistant `Cartes exportées` sur
  le Bureau. Le nom reprend `<valeur de mana> - <nom français>` de l'export de
  liste, avec suffixe de face et de doublon si nécessaire. La visionneuse
  commune à Recherche, Éditions et Decks expose ce bouton entre import et verso.
- `GET /api/v2/deferred` et `/archive` pour les séries différées et les fichiers
  anciens non attribués. `/archive` exclut les chemins d'archives enregistrés
  comme résolus dans l'état de reprise local, sans modifier l'index historique.
- `POST /api/v2/archive/ocr` conserve le résultat par image dans l'état local.
  Une preuve unique comprenant titre, code d'édition, numéro et langue importe
  automatiquement dans un slot vide, puis retire l'original après vérification
  de l'image prête. Le lot OCR emploie dix requêtes simultanées au maximum et
  conserve les résultats terminés si l'utilisateur l'arrête. Les cas incertains
  ou les slots déjà prêts restent visibles pour revue manuelle.
- Chaque tuile d'archive présente le nom de carte suggéré ou déduit du nom de
  fichier et le nom du fichier. Après la tentative OCR, si l'import automatique
  échoue, elle dévoile le formulaire manuel dont le champ de recherche occupe
  une ligne entière. Le texte OCR brut et les
  métadonnées d'inventaire ne sont pas affichés. Un endpoint local fournit le
  SHA-256 à l'import manuel sans imposer une nouvelle lecture OCR.
- Une page d'Archive affiche au plus 50 fichiers locaux. Ses vignettes sont
  chargées dès la sélection de la page, y compris celles plus bas dans la grille.
  Une réponse de liste arrivée après une navigation plus récente ne remplace pas
  les tuiles et la pagination courantes.
- Après un import manuel ou OCR réussi, la tuile reste à sa place dans la page
  ouverte, indique que l'image a été importée et ne propose plus d'action. Le
  prochain chargement de la liste retire les chemins résolus. Les noms déjà
  enregistrés pour les autres archives restent présents après chaque import.
- Chaque tuile expose le bouton OCR ; après une lecture OCR réussie, le bouton
  OCR disparaît et le bouton Google Lens apparaît (nouvel onglet, image envoyée
  sur demande explicite). Un endpoint local fournit le SHA-256 à l'import
  manuel sans imposer une nouvelle lecture OCR.
- Un bouton « recherche automatique du nom » (loupe) est placé à gauche de la
  corbeille sur chaque tuile : `POST /api/v2/archive/auto-name` lit l'archive
  par OCR (cache partagé) puis décide du nom côté serveur. Si le nom courant
  (nom enregistré s'il existe, sinon celui dérivé du fichier) est trouvable
  par recherche texte dans le catalogue actif, il est conservé. Sinon le nom
  devient : le titre du catalogue d'une proposition OCR unique, sinon le nom
  du catalogue le plus proche du titre OCR lu (seuil de forte similarité),
  sinon la ligne lisible du titre OCR nettoyée (ponctuation de bord retirée,
  jetons d'un seul caractère ignorés). Ce nom est enregistré dans l'état
  local des archives résolues (`display_names`), renvoyé par la liste et par
  la réponse OCR (`display_name`), et la tuile l'affiche à la place du nom de
  fichier. La décision est idempotente : un fichier déjà nommé trouvable
  renvoie `kept`. Le nom est un instrument de recherche : il ne prouve jamais
  l'identité de l'impression. Après un nommage réussi, la tuile affiche la
  zone d'intégration (recherche manuelle, choix d'impression, import par
  face) comme après une lecture OCR : bouton OCR masqué, bouton Google Lens
  visible.
- À côté du bouton « Analyser toutes les archives avec OCR », un bouton
  « Rechercher tous les noms automatiquement » enchaîne la loupe sur toutes
  les archives (10 workers, progression, arrêt possible ; le bouton Arrêter
  stoppe l'un ou l'autre lot, les lectures déjà lancées se terminent). Une
  tuile visible est mise à jour en direct ; les autres le sont au
  rechargement de la liste depuis `display_names`.
- Une tuile dont l'OCR est déjà connu (résultat en cache valide) n'affiche
  pas le bouton « Lire le texte avec OCR » : le bouton Google Lens et la zone
  d'intégration apparaissent d'emblée avec le nom enregistré prérempli.
- Un bouton corbeille en haut à droite de chaque tuile supprime le fichier
  d'archive après confirmation explicite : `POST /api/v2/archive/delete`
  revalide le chemin (racine autorisée, image, taille, pas de lien
  symbolique, hors `Impossible/`), refuse pendant une maintenance ou un
  téléchargement groupé, retire le fichier et enregistre le retrait dans
  l'état des archives résolues avec le statut `discarded` et sa méthode.
- Les résultats de recherche manuelle d'archive incluent les slots image par
  face (`image_slots`) ; le sélecteur de face d'une impression choisie
  présélectionne le recto s'il manque, sinon la première face sans image
  prête (verso), sinon le recto en mode remplacement.
- Les résultats de recherche manuelle d'archive affichent le symbole d'édition
  fourni par `icon_svg_uri` dans les métadonnées Scryfall locales, à côté du
  code et du numéro. Seules les URL HTTPS du domaine SVG Scryfall sont admises.
  Sans filtre de code d'édition et pour une langue non anglaise, la recherche
  ajoute aux impressions nommées les impressions FR/EN du même oracle dans des
  éditions absentes des résultats ; chaque édition garde une ligne, FR en
  priorité puis EN. Ces lignes porteuses d'expansion portent
  `match_source=oracle_language_expansion` et restent de simples suggestions :
  l'identité d'une impression n'est jamais prouvée par le nom seul.
  Pour la recherche visuelle Google, le serveur local prépare sur clic explicite
  un JPEG normalisé de l'archive. Une page de transit locale dans le nouvel
  onglet, sandboxée par CSP à origine opaque, soumet `encoded_image` en multipart ;
  Google refuse l'upload dont l'origine est `127.0.0.1`.
  Seule cette page contrôlée autorise le script de transit et le POST vers Google.
  Un POST local soumis aux contrôles d'origine remet un ticket aléatoire valable
  une seule fois et brièvement ; la page de transit n'accepte que ce ticket.
  L'interface affiche une erreur si la préparation de l'image échoue.
  Les vignettes de l'archive et des impressions proposées affichent au survol
  ou au focus clavier une image agrandie, positionnée dans le viewport pour ne
  pas être coupée par la grille ou la liste défilante.
- Dans une proposition d'archive, le titre conserve toute la largeur disponible
  même si l'impression n'a pas encore d'image ; le symbole d'édition est alors
  la vignette principale. La confirmation manuelle n'expose pas de sélecteur
  d'action : elle importe l'archive dans le slot choisi, en remplaçant l'image
  prête si ce slot en possède une déjà, après une confirmation explicite.
- `GET /assets?path=...` pour les WebP gérés et `/api/v2/archive-image?path=...`
  pour un aperçu historique.
- `POST /api/v2/download` et `/api/v2/download/cancel` pour une face sélectionnée,
  avec `provider_card_id`, `face_index` et `task_id`.
- L'onglet Decks lit une base locale de produits préconstruits et la composition
  du deck choisi. Un lancement explicite crée une tâche d'import asynchrone,
  consultable et annulable depuis l'interface. Chaque entrée résolue garde
  l'identité d'impression exacte et passe par le téléchargeur d'image du catalogue
  actif ; un échec ou une impression absente est visible dans le rapport du deck.
  Le bouton de téléchargement est désactivé si aucune impression n'est dans le
  catalogue actif, avec une explication et une invitation à actualiser ce
  catalogue. Les impressions non indexées d'un deck partiel sont distinguées des
  véritables échecs réseau ou image.
- Les cartes d'une composition montrent leur vignette déjà stockée et leur nom,
  avec la fiche exacte au clic. La section des commandants précède le deck
  principal ; les jetons sont séparés en fin de liste. Les préconstruits
  reprennent la mosaïque des decks personnels. Chaque tuile affiche la
  couverture, le nom, les couleurs et une ligne avec le nom de l’édition et son
  icône lorsqu’elle est connue. L’API fournit l’URL Scryfall validée de l’icône.
  Les mosaïques de Recherche, Éditions, Decks et Perso ne proposent pas
  d’agrandissement au survol.
- La page d'une édition liste chaque impression et son numéro de collection
  dans cette édition, avant pagination. Elle privilégie une ligne française de cette
  impression ; à défaut, une ligne anglaise, puis japonaise, phyrexiane ou une
  autre langue disponible. Chaque vignette montre l'image de
  l'impression choisie et son nom dans la langue retenue en dessous, comme dans
  une composition de deck. Le clic ouvre la même visionneuse ; les flèches
  parcourent l'ordre des impressions, y compris entre pages. La liste et sa pagination
  restent vides tant qu'aucune édition n'est choisie. Une page d'édition contient
  au plus 300 cartes ; la recherche utilise la même grille et la même limite. L'encart des
  éditions différées n'apparaît pas sur cette page.
- Les champs « Rechercher une édition » des vues Recherche, Éditions et Decks
  utilisent la même recherche insensible aux accents, sur les noms français et
  anglais ainsi que sur le code/diminutif de chaque édition. Les résultats
  privilégient les préfixes et affichent au plus huit suggestions. Dans Decks,
  les suggestions restent limitées aux éditions effectivement représentées par
  les decks indexés.
- Le sélecteur d'éditions conserve la liste complète des groupes, ordonnée par
  sortie décroissante de leur édition racine. Les séries sont chargées une seule fois pour
  la session et partagée par l'onglet Éditions et le sélecteur d'édition de la
  vue Recherche ; son absence empêcherait aussi les suggestions. La recherche
  attenante ne modifie jamais ce sélecteur : elle propose au plus huit groupes
  sous son propre champ, par nom français/anglais ou code de groupe et de
  sous-édition. Choisir une
  suggestion sélectionne le groupe dans la liste complète et ouvre ses cartes.
  La sélection et la recherche sont conservées dans le fragment d'URL.
- Évolution de la navigation : les sélecteurs Éditions et Recherche regroupent
  les sous-éditions dont le nom établit explicitement un lien (Commander,
  jetons, promos, séries d'art, Alchemy). Le groupe transmet ses codes exacts
  à l'API ; la fiche conserve le code et l'identité de l'impression réelle.
  Une recherche globale par nom ou code d'édition retrouve aussi ses cartes.
- La fiche de carte est une visionneuse centrée sur l'image locale, sans texte
  de règles, coût de mana, provenance, dimensions ni détails techniques visibles.
  L'image occupe au plus l'espace disponible dans la visionneuse, avec
  réduction quand la fenêtre est plus petite et une marge sous la carte.
  Les commandes d'image et la fermeture montrent leur libellé au survol ou au
  focus clavier ; la fermeture reprend le style rond et lumineux des commandes.
  Son en-tête conserve seulement l'édition et le numéro d'impression. Pour une carte à plusieurs images
  physiques, un bouton rond sous l'import local alterne recto et verso. L'import
  local et ce bouton utilisent des icônes et les dimensions de la loupe.
- Depuis un deck, une édition ou des résultats de recherche, la fiche mémorise
  l'ordre exact de la liste ouverte. Les flèches gauche/droite parcourent cet
  ordre sans boutons ni compteur visibles, y compris les pages voisines des
  résultats paginés ; aucune fiche ne se substitue à une autre édition par son
  seul nom. Les touches sont ignorées dans un champ éditable ou une confirmation.
- Le bouton de recherche d'image, compact et accessible, est placé en haut à
  gauche de la visionneuse. Une confirmation explicite précède le POST de
  recherche seulement si le slot possède déjà une image. La commande de réparation générique et les détails de provenance
  ne figurent pas dans la fiche. La recherche prépare une candidate sans modifier
  l'image actuelle, puis une vue de comparaison laisse choisir « conserver » ou
  « utiliser la nouvelle ». Le choix de la nouvelle image actualise la fiche
  sans perdre le contexte de navigation.
- La recherche d’image est une tâche serveur suivie dans les notifications, même
  lorsque la visionneuse ou la comparaison est fermée. Fermer masque la fenêtre
  sans interrompre la recherche ; seul « Annuler » demande l’arrêt. Les actions
  de la visionneuse sont masquées pendant la recherche pour éviter les doublons.
  Une candidate terminée reste accessible dans les notifications jusqu’au choix
  explicite de conservation ou de remplacement. Le message de succès d’export
  vers le Bureau disparaît après quelques secondes.
  Publier une version IA issue de cette recherche clôt aussi la recherche source
  et retire sa candidate temporaire : sa notification « À vérifier » disparaît.
  Plusieurs recherches peuvent attendre simultanément : si l'utilisateur regarde
  encore la carte où il a lancé la recherche et qu'aucune autre modale n'est
  ouverte, la comparaison s'ouvre dès la fin du chargement. Sinon, le résultat
  attend dans la cloche sans remplacer le choix en cours. Chaque notification
  « À vérifier » rouvre les candidates de sa propre tâche, même après un
  rechargement de la page tant que le serveur conserve ces candidates.
- La recherche approfondie ouvre une seconde modale au-dessus de la visionneuse
  et affiche une grille de variantes FR/EN distinctes de la source actuelle,
  avec langue et dimensions d'origine sous chaque carte. Une zone séparée
  affiche la carte complète au survol, au focus ou au clic ; la validation publie
  uniquement la variante explicitement sélectionnée.
- Si la face affichée n'a aucun asset, une candidate validée est publiée dès
  qu'elle est trouvée sans confirmation de recherche ; la fiche et la grille
  active sont relues. La comparaison avec choix ancien/nouveau demeure pour une
  face qui possède déjà une image. Un aperçu seul ne vaut jamais publication.
- Une recherche d'image depuis la visionneuse télécharge aussi les autres faces
  manquantes de la même impression. Les imports de listes et de decks demandent
  déjà toutes les faces physiques avec `download_printing`.
- Chaque onglet possède un fragment d'URL distinct. Le fragment conserve aussi
  l'état pertinent de la vue (filtres, tri, sélection et page ; deck ouvert le cas
  échéant) afin qu'une actualisation ou l'historique restaure les mêmes critères.
  Dans Recherche, la restauration de l'URL ne lance jamais de requête : seul le
  bouton « Rechercher » lance une nouvelle recherche ; Entrée dans un champ ne
  soumet pas le formulaire. Les boutons de pagination chargent la page demandée
  après une recherche explicite. Une actualisation d'images après un téléchargement
  ou une mise à jour relit uniquement les slots des cartes affichées.
  Les modifications non soumises du formulaire Recherche sont conservées dans le
  stockage local du navigateur, séparément des critères appliqués par l'URL. Le
  démarrage ne doit pas écraser une saisie faite pendant le chargement initial.
  Le champ de nom conserve jusqu'à dix termes récents et les propose au focus,
  sans lancer la recherche avant l'action explicite de l'utilisateur.
- Au chargement direct d'un fragment d'URL, aucune vue n'est affichée par défaut
  avant l'initialisation JavaScript. L'onglet demandé est activé aussitôt, puis
  ses critères sont restaurés sans passage visible par l'accueil. La vue Recherche
  attend une action explicite sur « Rechercher » avant tout appel de recherche.
- La visionneuse possède des dimensions stables liées au viewport, sans
  défilement interne. Les zones d'en-tête, d'image et de contrôle gardent leurs
  dimensions pendant les GET de carte ; un indicateur de chargement occupe
  l'emplacement de l'image jusqu'à son décodage. Aucun texte de remplacement ni
  en-tête de la nouvelle fiche n'apparaît avant l'image. L'image et les commandes
  diminuent ensemble lorsque la fenêtre se rétrécit.
- La comparaison possède une zone de grand aperçu stable ; survoler ou focaliser
  la vignette ancienne ou nouvelle y affiche cette variante. Les choix restent
  accessibles au clavier et sur écran tactile. Fermer ou conserver l'ancienne
  image ne change aucun slot.
- `POST /api/v2/image/preview` prépare une candidate liée à un ID fournisseur,
  un `printing_id`, une face et une version active ; `GET /api/v2/image/candidate`
  sert temporairement son image depuis une clé opaque. `POST
  /api/v2/image/candidate/choose` valide « old » ou « new ». Le serveur borne le
  nombre de candidates en mémoire et leur durée de vie ; une version ou un slot
  modifié pendant la comparaison rend la validation caduque.
- `GET /api/v2/deck` enrichit chaque impression de son état d'image et expose
  `media_summary` sur les impressions uniques, jetons inclus. Le bouton de la
  modale demande les seules images manquantes ; quand toutes les faces sont
  prêtes, `POST /api/v2/deck/export` crée sur le Bureau un nouveau dossier de
  PNG nommés par valeur de mana et nom français (avec suffixe pour les copies),
  comprenant commandant, cartes et jetons. Les chemins de sortie sont validés
  et les fichiers préexistants ne sont pas écrasés.
- La liste des decks se filtre par format, type de produit et couleurs, et se trie
  par nom ou date. Une indexation compacte des couleurs du catalogue est lancée
  en arrière-plan à l'ouverture de l'onglet si la couverture est incomplète ;
  les cases de couleur attendent sa fin pour éviter des résultats partiels.
  `GET /api/v2/decks/filters` fournit les facettes et la couverture mesurée.
  L'interface affiche l'avancement et les erreurs de cette indexation.
  L'onglet n'interroge pas la liste des decks avant un clic sur Rechercher.
  Les changements de filtres préparent la demande sans modifier les résultats
  affichés ; Effacer les filtres revient à l'état initial sans message sous le
  formulaire. Chaque page de
  résultats contient au plus 200 decks, avec pagination sur la recherche validée.
- Dans l'interface, le texte de recherche des decks porte uniquement sur le nom
  français ou anglais. Les suggestions ouvrent directement le deck choisi. Le
  sélecteur d'édition des vues Recherche et Decks dispose du même champ de
  suggestions que la vue Éditions, sans réduire la liste complète. Le sélecteur
  des Decks ne contient que les éditions représentées dans les decks et suit le
  même ordre chronologique décroissant ; l'API des facettes expose `released_at`
  pour chaque édition afin que l'interface n'ait pas à retrier.
- Les intitulés de decks et d'éditions affichent le français prouvé, suivi du
  titre anglais du deck lorsqu'il diffère. La sélection d'édition transmet son
  code stable ; les types de produit gardent leur valeur anglaise en API mais
  s'affichent en français. La composition préconstruite s’ouvre dans l’onglet
  Decks et reste ouverte au changement d’onglet. Retour à la liste réaffiche
  les résultats déjà chargés au même emplacement de défilement. Le détail
  affiche le type, le nom et les couleurs, puis les actions Retour à la liste,
  Exporter et Créer un deck perso avant la composition.
- Chaque onglet mémorise sa position de défilement pendant la session. Une
  navigation entre onglets conserve les mosaïques déjà chargées. Les changements
  de sélection, la pagination et les actions de rafraîchissement peuvent relire
  les résultats.
- Recherche de cartes et de decks utilisent six sélecteurs visuels W/U/B/R/G/C.
  Les couleurs cochées sont cumulatives ; C signifie strictement sans couleur
  et se sélectionne seul. À droite des symboles, le mode « Normal » conserve la
  recherche par inclusion des couleurs choisies ; le mode « Couleurs exactes »
  exige exactement cet ensemble de couleurs (mono W, bicolore WU, etc.). Sans
  couleur choisie, les deux modes ne filtrent pas. Les symboles gardent un nom
  accessible en français. L'état sélectionné reste visuellement stable au clic,
  sans halo de focus de souris ; le focus clavier demeure visible.
- Recherche, Éditions et Decks partagent les marges, la typographie, le panneau
  de filtres et les deux actions d'en-tête. Le libellé de téléchargement reste
  sur une ligne et le rafraîchissement conserve un bouton carré.
- Recherche de cartes et Decks partagent le style compact du filtre de couleurs,
  avec les symboles de mana et le mode sur la même ligne lorsque la largeur
  le permet. Un séparateur introduit le tri et les filtres complémentaires. La
  recherche de cartes conserve un seul bouton « Effacer les filtres »
  pour réinitialiser tous les critères, y compris l'affinage. Recherche de deck
  et filtres sont présentés dans un formulaire unique ; le nom du deck et le
  sélecteur/recherche d'édition partagent sa première ligne.
- Une fiche propose une nouvelle recherche ciblée même si son image est déjà
  `ready`. Cette action ne peut remplacer l'asset que par un candidat de la même
  impression et face, avec provenance et qualité contrôlées ; elle attend une
  confirmation de l'utilisateur avant l'appel réseau.
- La modale de recherche d'image présente d'abord une grille responsive des
  variantes et un choix de remplacement classique. Le bouton « Créer une version
  IA » ouvre un parcours distinct : sélection d'une référence visuelle anglaise,
  puis d'une référence textuelle française, puis génération et revue côte à côte
  des deux références et de la candidate IA. Le prompt de correction et la
  validation ne sont montrés qu'à l'étape de revue. La référence visuelle reste
  limitée à la même impression et face. La référence textuelle peut provenir d'un
  asset FR local au même nom ou Oracle si sa structure et sa face concordent. La
  sélection est dans un panneau à
  gauche; les cartes comparées ou la référence agrandie sont à droite. Les images
  doivent toujours rester entières, y compris le bas de la carte; le panneau de
  miniatures défile verticalement sans imposer de défilement horizontal. La
  comparaison classique agrandit les deux cartes dans tout le panneau de droite
  et garde Annuler, Valider et Créer une version IA au bas du panneau de gauche.
  Dans les deux sélections de référence IA, la carte cliquée est affichée en grand
  dans le cadre fixe de droite, réduite par `object-fit: contain` quelle que soit
  sa résolution source.
  L'asset déjà intégré au slot exact reste proposé parmi les références même
  quand la recherche trouve d'autres images ; `visual_only` l'empêche seulement
  de devenir un nouvel asset publié. Le choix du texte français précontrôle la
  base locale et retient une variante compatible si plusieurs textes officiels
  diffèrent. En l'absence de texte officiel, un avertissement, une zone de prompt
  facultatif et les actions retour/annuler ou envoi quand même précèdent la
  génération dans une modale centrée dédiée, qui remplace la sélection pendant
  cette étape. L'envoi sans traduction conserve ce mode et le prompt en correction
  ainsi qu'en revue rouverte depuis une notification. Le sélecteur textuel inclut
  l'original intégré même si sa langue d'import est incertaine et les assets FR
  prêts d'autres impressions au même nom (ou au même Oracle), avec layout et
  structure de faces compatibles. Ces images sont des références de texte
  seulement : Codex en transcrit les champs, puis la génération reçoit uniquement
  l'image visuelle et cette transcription. La publication reste liée à
  l'impression cible. À l'étape 2, la vignette choisie garde le même contour de
  sélection qu'à l'étape 1.
  La revue d'une génération réussie utilise toute la largeur de la modale pour
  les cartes de référence et la candidate IA côte à côte, entièrement visibles
  avec `object-fit: contain` sans barre de défilement qui coupe l'image. La zone
  de correction et les actions de revue partagent une ligne en vue large, sous
  les cartes ; le message d'état ne prend de place que pendant un traitement ou
  une erreur. La consigne de correction du parcours initial n'apparaît qu'après
  Relancer. Une correction lancée depuis la revue reprend le même écran de
  chargement que la première génération, avec retour à la consultation et
  annulation explicite. Si le fournisseur
  échoue, une étape d'erreur affiche sa cause et un bouton ramène à la sélection
  initiale des images; aucune carte IA absente n'est présentée comme résultat.
- Le serveur vérifie si une instance locale répond déjà à `/api/v2/health` avant
  de l’ouvrir ou de démarrer un nouveau port.

## Flux et invariants

1. Chaque requête de catalogue résout `current.json`, puis ouvre la base pointée
   en mode SQLite `ro`.
2. La recherche FTS5 cite les mots saisis pour éviter des opérateurs injectés ;
   le nom alternatif imprimé sur une carte participe aussi à la recherche
   textuelle lorsqu'il est présent dans les données source. Une recherche de
   nom propre doit retrouver ces impressions.
   chaque mot accepte un début de mot afin que les noms et capacités partiels
   trouvent les textes français ou anglais. La ligne affichée privilégie FR,
   puis EN, JA et les autres langues disponibles ; il n'y a pas de filtre de
   langue utilisateur dans Recherche. Les recherches fréquentes doivent rester utilisables sur le
   catalogue actif. Le filtre de type principal utilise les colonnes FTS5
   `type_line` pour réduire d'abord les impressions candidates ; les filtres SQL
   vérifient ensuite le type et les autres critères.
   Un terme n'est traité comme édition que s'il égale son code ou son nom : une
   capacité comme « vol » ne doit pas être capturée par « Revolt ». Chaque
   changement de filtre ou de texte prépare seulement la demande ; le bouton
   « Rechercher » lance explicitement le moteur unifié. Les critères principaux
   et secondaires sont soumis ensemble, sans recherche préalable ; une séparation
   visuelle distingue une section secondaire toujours visible. Le nom, le
   sélecteur d'édition et la recherche d'édition sont alignés sur une ligne
   lorsque la largeur le permet.
   Le type de carte principal est choisi parmi les types disponibles dans un
   sélecteur. La valeur de mana exacte et le filtre de langue ne sont pas proposés
   dans l'interface ; le mana se filtre par bornes dans la section secondaire.
   Les critères secondaires comprennent le filtre d'image, les bornes de mana,
   force et endurance. Le tri choisit séparément le
   critère (édition, ajout, mana, force ou endurance) et le sens ascendant ou
   descendant. Les bornes s'appliquent
   avant pagination ; les forces/endurance non numériques sont exclues quand
   une borne correspondante est demandée.
   Les filtres SQL utilisent des paramètres. Après filtrage, elle conserve
   chaque impression exacte (les artworks/variantes distincts restent visibles)
   et choisit une seule ligne de langue par impression : FR puis EN puis JA, et
   pagine au plus 200 résultats dans Recherche. Le panneau filtre les cartes
   ayant au moins un slot image prêt, celles sans aucun slot prêt, ou les deux,
   avant déduplication des lignes de langue et pagination. La sélection candidate (FTS, filtres,
   déduplication) est gardée dans un cache mémoire borné, indexé par la version
   et l'empreinte SQLite ainsi que les critères hors tri/page ; changer le sens
   du tri ou de page réutilise donc cette sélection. Le tri ajoute l'édition ancienne et
   l'ordre d'ajout croissant ou décroissant. Avec image uniquement, la date
   d'ajout est celle du WebP lié au slot, lue une seule fois par asset pendant
   le classement ; autrement, c'est la première
   apparition de l'impression dans une publication. Le catalogue antérieur
   à ce suivi forme un seul lot lors de sa première mise à jour ; la date de
   sortie de l'édition, son code, le numéro et l'identifiant d'impression
   départagent ces cartes dans le sens de tri choisi. Une correspondance par nom FR ou EN
   peut étendre les résultats aux impressions du même Oracle dans les autres
   éditions éligibles ; édition et filtres de carte restent appliqués. Chaque
   impression exacte choisit une ligne FR, puis EN, puis JA, puis une autre
   langue disponible. Les images restent attachées à leur impression exacte,
   y compris les images importées ou retouchées localement. Les recherches non fondées sur un nom ne sont pas
   étendues par Oracle. La lecture des lignes affichées est groupée par page.
   Le filtre couleur des cartes accepte plusieurs couleurs indépendantes ou C
   pour `colors_key=''`, jamais une chaîne sous-texte ambiguë. `color_mode=exact`
   compare la clé canonique de couleurs complète ; `contains` (défaut) vérifie
   chaque couleur demandée. Le filtre deck suit la même logique sur l'identité
   de couleur indexée du deck.
3. Une fiche est identifiée par ID fournisseur et reste liée à son `printing_id`.
   Les traductions FR et EN proviennent de cette impression exacte.
4. Les slots image sont enrichis depuis `image_slots` et `image_assets`; les
   fichiers servis sont résolus strictement sous la racine autorisée.
5. Le serveur est lié à `127.0.0.1`, envoie CSP et `nosniff`. Les réponses API
   restent `no-store`. Les WebP de `/assets` sont identifiés par leur hash de
   contenu et peuvent employer un cache navigateur privé d'un an `immutable` ;
   ils restent résolus et autorisés via l'asset enregistré en base.
   refuse les POST provenant d’une autre origine.
6. Les actions de recherche ne contactent pas les fournisseurs ; seuls le
   téléchargement, l'import de deck ou la recherche ciblée demandés par
   l’utilisateur déclenchent des GET d'images.

## Dépendances et risques

- [Catalogue](catalogue.manifest.md) pour les tables et FTS5 ;
  [maintenance](maintenance-publication.manifest.md) pour le pointeur ;
  [images](images.manifest.md) pour les POST ;
  [historique](historique.manifest.md) pour l’archive.
- Une version absente ou un `current.json` incohérent empêche la consultation. Le
  traitement des erreurs doit conserver une réponse HTTP explicite.
- La date dans `/api/v2/stats` est actuellement `null` sur le snapshot observé,
  malgré `source_as_of` en base ; voir [audit](../audit.md).

## Points à vérifier lors d’une intervention

- Contrôler la réponse de santé, la recherche en FR et EN, le filtre d’édition et
  l’ouverture d’une impression exacte.
- Tester un chemin d’asset invalide et une origine POST étrangère, puis confirmer
  que les données locales ne sont pas exposées.
- Vérifier l’affichage distinct du texte disponible et du slot image absent ; ne
  pas confondre complétude textuelle et couverture d’images.

## Évolution autorisée — decks personnels (18 novembre 2026)

- Ajouter Perso entre Decks et Archives. L'éditeur conserve le deck actif lors
  des allers-retours Recherche/Perso ; un retour explicite ouvre la liste.
- Les decks personnels portent un UUID, un nom, des couleurs W/U/B/R/G (vide
  pour incolore), une révision et des sections ordonnées identifiées par UUID.
  Les entrées conservent les identités exactes fournisseur/impression, leur
  quantité et les métadonnées utiles à la consultation hors catalogue futur.
- Stocker les compositions dans une SQLite dédiée sous `_bibliotheque_mtg/state/`,
  indépendante des versions du catalogue. Transactions durables, historique des
  révisions et contrôle de concurrence évitent les écrasements silencieux.
- L'API locale expose liste, création, lecture, sauvegarde, ajout d'une impression,
  copie d'un préconstruit et export Bureau. Tous les POST suivent le contrôle
  d'origine existant ; aucune requête ne reçoit de chemin de fichier libre.
- La visionneuse commune propose un plus entre l'export image et le verso,
  ouvrant la liste des decks personnels. Ajouter une carte ne ferme pas la fiche.
- L'éditeur offre nom, couleurs, sections ajoutables/renommables/supprimables,
  quantités, retrait des cartes et déplacement par glisser-déposer ou sélecteur
  accessible. Supprimer une section non vide déplace ses cartes vers une section
  conservée ; aucune suppression de section ne détruit implicitement les cartes.
- La première entrée de la première section représente la couverture. Les sections
  initiales sont Commandant (si pertinent), Créatures, Rituels, Éphémères,
  Artefacts, Enchantements, Planeswalkers, Terrains, Jetons ; conserver aussi les
  autres sections d'un préconstruit (réserve, etc.) et toutes ses quantités.
- L'import préconstruit crée une copie indépendante, conserve nom/couleurs et
  identités disponibles, sans téléchargement d'image implicite. Les cartes non
  résolues restent visibles et ne sont jamais supprimées silencieusement.
- Rechercher la liste personnelle par nom et couleurs. L'export réutilise le
  parcours PNG sur le Bureau des decks préconstruits et n'écrase aucun fichier.
- Réutiliser HTML/CSS/JS natif, sqlite3 et la palette violette existante ; aucune
  dépendance supplémentaire. Montrer l'état de sauvegarde et les erreurs ; ne
  jamais afficher « enregistré » avant confirmation serveur.

### Ajustement utilisateur — consultation et édition

- Ouvrir les decks personnels en consultation graphique : titres et images,
  sans champs ni commandes de classement permanentes. « Modifier » active les
  champs/quantités/retraits/sections ; « Terminer » attend la sauvegarde confirmée.
- Déplacer les cartes exclusivement par glisser-déposer en édition, dans une
  section ou entre sections. Retirer les flèches haut/bas et sélecteurs de déplacement.
- Aucune sélection spéciale du commandant : la première carte de la section de
  commandement identifiée par `source_section=commander` est la couverture, quel
  que soit l'ordre des sections. Cette zone peut contenir plusieurs cartes.
  Pour un deck sans zone de commandement, utiliser la première section.
- La création manuelle prévoit une zone de commandement sans case à cocher.
  Une fois créé, le deck apparaît dans la liste sans ouvrir son détail.
  L'import d'un deck non Commander ne crée pas cette zone.
- Ces précisions remplacent les alternatives de classement et la règle initiale
  de couverture décrites dans l'évolution ci-dessus.

### Taille des aperçus

- Deux commandes Lucide dans la barre supérieure règlent la taille des vignettes
  des mosaïques et des couvertures de decks dans tous les onglets, y compris
  les compositions en consultation ou en édition. Dans les mosaïques, le
  réglage mémorise le nombre de colonnes souhaité, de trois à neuf. Les colonnes se partagent
  toute la largeur et leur nombre baisse si la fenêtre est trop étroite.
  Les textes et symboles s’adaptent à la largeur réelle de la vignette.
  La visionneuse conserve ses dimensions liées à la fenêtre.

### Ajustement — présentation et actions des decks personnels

- Les compositions personnelles reprennent l’en-tête des préconstruits avec le
  nom et les couleurs, ainsi que leurs séparateurs et titres de section avec le
  nombre de cartes entre parenthèses. Aucun total général n’est affiché.
  Actualiser relit le deck et les images locales.
- Réutiliser la présentation des filtres Recherche, avec Incolore exclusif,
  correspondance normale ou exacte et tri. La création est hors des filtres.
- En édition, le glisser-déposer fait défiler la page près des bords.
  Dans la section source, les cartes voisines se décalent pour prévisualiser
  le point de dépôt. Seul le dépôt valide le nouvel ordre.
- Retirer une carte du deck demande une confirmation avant de modifier la liste.
- La suppression explicite retire le deck de la liste et conserve ses révisions.
  Elle vérifie la révision courante et utilise la même protection POST locale.
- Après un ajout réussi, fermer le sélecteur et afficher un message bref sur
  la fiche. Afficher les erreurs dans le sélecteur, avec Fermer centré en bas.

### Consultation des utilisations d’une carte

- `GET /api/v2/card/usage?id=<provider_card_id>` renvoie `found`, `editions`, `decks`, `personal_decks` et `unloaded_decks`. Les rapprochements utilisent l'identité oracle du catalogue actif, ou l'impression exacte si elle n'a pas d'oracle. Les éditions éligibles sont regroupées par code. Les decks préconstruits sont limités aux compositions locales chargées ; `unloaded_decks` signale les compositions non chargées. Les rapprochements de leurs entrées emploient un identifiant Scryfall, une identité oracle explicite ou une édition et un numéro non ambigus. Les decks personnels utilisent les impressions conservées dans leurs compositions actives. Aucun accès réseau ni chargement implicite n'est effectué.
- La visionneuse ouvre la liste des éditions et des decks associés dans une
  modale ; chaque lien s'ouvre dans un nouvel onglet à la destination précise.
  Cette modale propose deux vues : impression précise (même `printing_id`) et
  carte globale (même `oracle_id`, ou l'impression seule sans oracle). Les
  éditions et decks sont séparés dans chacune. Une édition ouverte par ce
  parcours sélectionne son code exact, sans regroupement automatique ; un deck
  s'ouvre directement sur son détail. Le bouton d'accès reste en bas à gauche
  de la visionneuse. La modale utilise la palette violette, et un clic sur son
  arrière-plan la ferme pour revenir à la visionneuse de carte.
  Les brouillons de decks personnels sont isolés par onglet via sessionStorage.
  Au retour sur un onglet Perso, un deck consulté sans modification locale est
  relu si sa révision a changé ; une édition active est préservée et avertie.
