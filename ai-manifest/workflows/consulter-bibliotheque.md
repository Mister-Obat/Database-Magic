# Workflow — démarrer et consulter la bibliothèque

## Déclencheur et résultat

L’utilisateur ouvre `Lancer_Bibliotheque_Magic.bat` pour parcourir les
éditions et les impressions du snapshot V2 actif. Le résultat est une page locale
de recherche avec fiche, traductions et état de chaque image.

## Préconditions

- `_bibliotheque_mtg/.venv/Scripts/python.exe` et
  `_bibliotheque_mtg/src/python/bibliotheque_mtg/` sont présents.
- `_bibliotheque_mtg/v3-staging/current.json` pointe vers une base et des
  exports existants.
- `MAGIC_LIBRARY_PORT` permet de choisir un autre port. Le lanceur ferme une
  instance locale compatible sur ce port ; il arrête une instance incompatible
  reconnue et refuse d'arrêter tout autre programme.

## Étapes

1. Le `.bat` délègue au script PowerShell local : il réutilise une instance
   compatible ou remplace une ancienne instance identifiée. Sinon il lance
   `python -m bibliotheque_mtg.local_app` de façon masquée, puis attend jusqu'à
   cinq minutes que `/api/v2/health` réponde. Un processus du même exécutable
   déjà en cours de démarrage est attendu plutôt que lancé une seconde fois.
   La fenêtre de commande se ferme ensuite.
2. Le serveur ouvre rapidement le catalogue actif en mode lecture et lie le
   `ThreadingHTTPServer` à `127.0.0.1`. La synchronisation exhaustive de
   `_bibliotheque_mtg/Images/` n'est pas exécutée au démarrage ; les images sont
   classées au moment des opérations qui les ajoutent ou les réparent.
3. La page charge le JavaScript/CSS locaux puis demande les statistiques et les
   éditions par l’API V2.
4. Une recherche utilise `GET /api/v2/search?q=...` avec nom/texte, édition,
   rareté, couleurs choisies par symboles W/U/B/R/G ou incolore exclusif, et type
   de carte choisi dans une liste. Le nom, le sélecteur d'édition et la recherche
   d'édition sont sur la ligne principale. La section « Affiner les résultats »
   reste visible sous un séparateur et ajoute filtre d'image, bornes de mana,
   force et endurance, ainsi qu'un critère et un sens de tri séparés. Les noms
   français et anglais peuvent correspondre ; l'affichage choisit FR puis EN, JA
   ou une autre langue disponible. Les résultats paginés portent leur ID
   fournisseur et leur `printing_id`.
5. `GET /api/v2/card?id=...` ouvre la fiche de cette ligne source, ses traductions
   de la même impression, ses faces et ses slots WebP.
   La fiche présente l'image au centre avec édition et numéro, permet de changer
   de face physique et d'aller à la carte précédente ou suivante de la liste
   consultée avec les boutons ou les flèches du clavier. Une recherche de
   meilleure image demande une confirmation avant tout appel réseau.
6. L’onglet d’archive lit les fichiers historiques non attribués par
   `/api/v2/archive`; ils restent séparés des cartes identifiées.
7. L’onglet Decks lit la base locale des préconstruits. La liste peut être
   filtrée par format, type de produit traduit à l’écran, édition et symboles de
   couleurs (dont incolore exclusif), en mode normal ou couleurs exactes, puis
   triée par nom ou date. Le nom français
   vérifié précède le nom anglais, et l’édition est affichée en toutes lettres
   avec son code. Le détail s’ouvre dans une modale au-dessus de la liste.
   Si les couleurs manquent, une tâche d'indexation compacte les prépare en
   arrière-plan avant d'activer les boutons de filtre. Ouvrir un deck charge sa
   composition si elle n'est pas encore en
   cache ; « Télécharger les images » démarre une tâche asynchrone qui écrit
   chaque impression prouvée dans les mêmes slots que la recherche par édition.
   La couverture des couleurs est affichée lorsque l'indexation est partielle.
   Si le catalogue publié ne contient aucune impression du deck sélectionné,
   le téléchargement est indisponible avec une explication invitant à actualiser
   le catalogue ; un deck partiellement indexé distingue les impressions absentes
   des échecs de téléchargement.
   La modale montre d'abord les commandants, puis les cartes avec leur image
   locale et enfin les jetons. Le téléchargement ne tente que les images encore
   manquantes. Quand toutes les faces sont prêtes, l'action devient un export
   PNG sur le Bureau dans un nouveau dossier nommé d'après le deck.

## Fichiers et invariants

- [Application et recherche](../modules/interface-recherche.manifest.md) utilise
  `local_app.py`, `v2/query.py` et `ui/`;
  [stockage](../infra/stockage-local.manifest.md) fournit la base courante.
- La navigation lit SQLite en mode `ro`; aucune recherche ne dépend d’une requête
  externe.
- L’édition différée n’est pas affichée parmi les éditions actives, mais son motif
  reste accessible dans l’API dédiée.
- Une fiche ne doit jamais afficher l’image d’une impression simplement homonyme ;
  le slot appartient au `printing_id` exact.
- Une image absente reste visiblement manquante, sans faux PNG blanc.

## Vérifications et cas d’échec

- Vérifier `http://127.0.0.1:8765/api/v2/health`, puis rechercher une carte FR et
  EN et ouvrir une fiche. Le rapport V2 a contrôlé « Foudre » en FR : 46 résultats
  et une fiche avec WebP FR.
- Si la page ne s’ouvre pas, vérifier le port et `current.json`. Si un autre
  programme utilise le port configuré, le lanceur le signale sans l'arrêter.
- Si une édition ou une carte manque, vérifier d’abord son éligibilité et son ID
  dans le [catalogue](../modules/catalogue.manifest.md).
- Si la date de fraîcheur apparaît vide, voir [l’écart connu](../audit.md) sur
  `catalogue_date` ; les données de recherche restent présentes.
