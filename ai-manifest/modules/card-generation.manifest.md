# Module — génération de carte complète par IA

## Responsabilité

Régénérer une carte Magic complète en français à partir d'une image visuelle et
soit du texte transcrit d'une image française, soit de métadonnées françaises
vérifiées, avec l’outil image natif du Codex CLI connecté localement. L’image
visuelle guide l’art, la composition et le cadre; l'image de référence textuelle
n'est jamais jointe à l'appel imagegen. Une image IA précédente peut être
jointe pour une tentative de correction. Le module produit une candidate PNG
pour revue explicite; il ne modifie pas le catalogue ou ses slots.

## Emplacement et contrat

- `bibliotheque_mtg/v2/card_generation.py` : résolution de Codex CLI, appel
  éphémère, validation de la sortie PNG et sauvegarde d’une candidate locale.
- `generate_card_candidate(visual_path, text_path, candidate_root,
  correction_prompt='', previous_path=None, ...)` effectue une seule tentative
  et retourne un identifiant opaque, le chemin de candidate, son SHA-256, les
  dimensions et la provenance fournisseur. L’interface pilote la boucle de
  correction et peut transmettre la candidate précédente.
- `extract_card_text_reference(image_path, translate_to_french=False, ...)`
  utilise un tour Codex textuel isolé pour transcrire les champs visibles de
  l'image de référence textuelle, puis valide sa réponse JSON. En cas de texte
  illisible ou de sortie invalide, la tâche échoue sans lancer imagegen. Une
  correction réutilise la transcription attachée à la candidate précédente.
- Le mode texte accepte une seule image visuelle, quelle que soit sa langue.
  L'application lit le nom, les capacités, les coûts, les types et autres champs
  de règles depuis les enregistrements locaux associés à l'impression ou au même
  identifiant Oracle, puis dans la base source locale
  `_bibliotheque_mtg/catalogue.sqlite` (`cards`, `lang='fr'`), qui conserve aussi
  les traductions des impressions absentes du catalogue publié. Une transcription
  officielle vérifiée peut aussi provenir de la table locale
  `official_french_text`, liée à l'ID fournisseur exact et à une image Wizards
  citée comme provenance. L'API Scryfall
  n'est pas interrogée pour trouver le texte.
  Seuls les champs français officiels déjà stockés (`printed_name`,
  `printed_type_line`, `printed_text`) peuvent servir de texte de sortie. Le
  texte Oracle anglais peut identifier la carte, mais ne doit jamais être
  traduit ou présenté comme texte français officiel. Une ligne FR
  d'une autre impression du même oracle peut être utilisée seulement si les
  champs pertinents et la structure des faces concordent. Si plusieurs textes
  compatibles diffèrent, l'impression exacte prime, puis la première variante
  dans l'ordre stable de la base est choisie sans bloquer la génération. Sa
  provenance de traduction inter-impressions est affichée. Dans le sélecteur
  d'images, une image française d'une autre impression au nom FR identique et à
  la même structure de faces peut aussi être choisie explicitement comme référence
  de texte. Elle n'est pas déclarée traduction officielle de l'impression cible,
  reste limitée à la référence IA et ne change jamais l'identité de publication.
  L'image actuelle du slot peut être proposée dans les références textuelles même
  si son asset a été importé avec une langue incertaine ; le modèle doit alors lire
  le texte de cette image et le traduire si nécessaire. Un texte introuvable
  arrête le mode de données locales jusqu'au choix explicite de traduction décrit
  ci-dessous ; une identité ambiguë arrête tous les modes avec un message clair,
  et aucun texte n'est inventé implicitement. La provenance
  et le texte résolus sont montrés pour la revue. Le prompt transmet ces champs
  textuels avec l'image et demande une carte française haute définition.
- Le parcours de choix IA expose aussi l'image actuellement intégrée dans le slot
  local validé, même si la recherche approfondie a trouvé d'autres variantes.
  Elle peut guider la référence visuelle et, si elle est française, la référence
  textuelle ; elle reste `visual_only` pour le choix de publication.
- Quand l'utilisateur choisit les informations de la base locale, l'interface
  précontrôle le texte français avant de démarrer imagegen. Si aucun texte officiel
  compatible n'existe, elle affiche l'avertissement « pas de traduction
  française officielle dans votre database », un prompt utilisateur facultatif,
  ainsi que les actions retour/annuler ou envoyer quand même. Le mode explicite
  d'envoi sans traduction demande alors au modèle de traduire lui-même le texte
  visible de la référence anglaise en français, de conserver l'illustration, le
  cadre et la mise en page, et de ne modifier que le texte de la carte. Le prompt
  facultatif n'est transmis que si l'utilisateur l'a renseigné. Une identité
  ambiguë, incompatible ou introuvable demeure une erreur distincte, jamais une
  autorisation implicite de traduction.
- Le mode de traduction IA et la consigne facultative restent attachés à la
  candidate lors des corrections et de la reprise de revue depuis les notifications.
- Avant toute première génération, le sélecteur visuel et le choix du texte
  (image française ou données officielles locales) exposent une consigne
  facultative de 4 000 caractères maximum. La sélection du texte officiel local
  prépare le mode sans lancer la génération ; l'utilisateur peut saisir sa
  consigne puis cliquer sur Générer. La même consigne est transmise au premier
  appel imagegen et reste préremplie pour les tentatives de correction.
- La consigne de correction est limitée à 4 000 caractères; la génération expire
  après 600 secondes par défaut. Chaque référence est un fichier image local
  déchiffrable de 20 MiB maximum et de 40 mégapixels maximum. Une référence de
  recherche de faible résolution reste admissible dès 32 px par côté; sa qualité
  est appréciée par l'utilisateur dans la modale. La sortie IA conserve la
  résolution du PNG généré et n'est pas réduite à celle de la référence FR.
  La candidate est un
  PNG décodable, de proportions proches d’une carte, et de 20 MiB maximum.
- La candidate est écrite atomiquement sous le dossier de candidates fourni par
  l’appelant. Seul un endpoint local explicite peut la rendre consultable. Toute
  publication dans la collection gérée exige un parcours distinct et un choix
  explicite de l’utilisateur.

## Fournisseur et sécurité

- Le CLI Codex installé est résolu depuis PATH puis depuis son installation
  Windows connue; aucun téléchargement ou installation n’est lancé.
- La session `auth.json` du profil actif est copiée dans un `CODEX_HOME` temporaire
  privé pour isoler la sortie. Les variables `OPENAI_API_KEY`, `CODEX_API_KEY` et
  `HF_API_KEY` ne sont pas transmises. Le CLI est lancé avec `--ephemeral`,
  `--sandbox read-only`, `--json` et l’outil `image_generation` activé.
- La référence textuelle, lorsqu'elle est une image, est envoyée uniquement au
  tour Codex de transcription structurée. Le tour imagegen reçoit la première
  image visuelle, les champs textuels validés et la candidate précédente
  facultative, jamais la seconde image. Le prompt demande
  une traduction guidée simplement par cette transcription lorsqu'une image
  française est choisie. Sans image française, il insiste sur la traduction
  directe des mots visibles sur l'original et sur les noms et textes d'ambiance
  propres à cette impression ; les champs de la base ne servent que d'aide.
  Si une consigne manuelle est fournie, elle prime sur ces règles automatiques :
  le prompt ne fixe plus que la première image comme référence visuelle, une
  carte française complète en haute définition et un seul appel imagegen. Les
  champs français éventuels restent une référence facultative subordonnée à la
  consigne de l'utilisateur.
  Le prompt demande aussi
  une édition localisée de la première carte : illustration, cadre, composition,
  mise en page et symbole d'édition strictement identiques ; seuls les mots
  imprimés nécessaires au français changent. La transcription française sert à
  la reproduction exacte du texte, et la candidate précédente sert de base à une
  éventuelle correction sans relâcher les invariants visuels.
- La consigne utilisateur est transmise par stdin; les messages d’erreur affichés
  sont assainis et ne reproduisent ni le prompt, ni les chemins locaux, ni la
  sortie brute du CLI.
- Si Codex produit plusieurs PNG nouveaux dans l’espace privé de l’invocation,
  le dernier reçu est proposé à la revue. Les échecs de quota ou de modération n’entraînent aucun changement
  de fournisseur et aucune publication.
- La génération lancée depuis la fiche est une tâche locale identifiable, visible
  dans les notifications pendant son exécution et après sa fin. Quitter la modale
  ne l'interrompt pas. L'utilisateur peut rouvrir son avancement ou sa candidate
  depuis la cloche et peut demander l'annulation explicite de la tâche. Cette
  annulation arrête le processus CLI associé et ne publie aucune image.
- Les références de prévisualisation restent en mémoire au plus 24 heures (jusqu'à
  100 ensembles) pour permettre une revue différée. Les tâches et références
  appartiennent au processus serveur actif ; un redémarrage du serveur les perd.
- La récupération vérifie d'abord les PNG du dossier privé, puis les chemins PNG
  annoncés par les événements JSONL qui restent dans ce même dossier, puis un
  unique payload PNG inline si nécessaire. Les diagnostics CLI sont classés en
  quota, limite temporaire, modération, accès refusé ou outil indisponible; le
  message utilisateur ne contient ni la sortie brute, ni les références, ni les
  chemins privés. Un échec ne crée aucune candidate montrée comme valide.
- Le texte exact est demandé au modèle image, mais le module ne réalise pas d’OCR
  de contrôle; toute candidate doit rester en comparaison/revue utilisateur et
  ne peut être présentée comme une transcription garantie.

## Recherche open source et adaptation

Le parcours Codex s’inspire de l’adaptateur `codex_images.py` du projet local
Traducteur BD et des exemples publics de génération via Codex CLI. La séparation
entre candidate et publication ainsi que l’isolation du dossier de sortie sont
conservées; le flux reçoit les deux références sélectionnées et ne publie aucune
image automatiquement. Référence comparable :
[openai/codex image-generation tool](https://github.com/openai/codex/blob/main/codex-rs/ext/image-generation/src/tool.rs).

## Vérification

Le smoke réel imagegen n’est pas exécuté par l’implémentation. Vérifier au minimum
la compilation Python; l’intégration HTTP doit traiter le résultat comme une
candidate privée et valider l’identifiant avant de servir ses octets.
