# Direction artistique de l’interface

Référence du 18 novembre 2026 pour Accueil, Recherche, Éditions, Decks et Archives. Cette direction s’applique à la navigation, aux vues principales, au panneau de notifications et au détail de deck. Pour une intervention graphique, lire aussi le [manifest de l’interface](interface-recherche.manifest.md) pour préserver les comportements. Les valeurs exécutées se trouvent dans [styles.css](../../_bibliotheque_mtg/src/python/bibliotheque_mtg/ui/styles.css) et les pictogrammes dans [index.html](../../_bibliotheque_mtg/src/python/bibliotheque_mtg/ui/index.html).

## Intention

Évoquer un catalogue de cartes Magic avec du verre fumé, des reflets violets froids et des surfaces sombres. Le contraste sert d’abord les titres, les champs et les actions. La profondeur vient des couches de couleur et de la lumière dans la surface. Garder le fond derrière les titres uni. Écarter les halos sous la navigation et les boutons, les contours lumineux épais ou asymétriques et le balayage blanc au survol. Le logo est un élément d’identité indépendant de la palette.

Une retouche de cette DA agit sur les couleurs, les contours et les états visuels. Toute évolution des fenêtres, des modales, de la disposition ou du comportement relève d’une demande distincte.

La visionneuse de carte, la recherche et la sélection d’images, la génération IA et sa confirmation de traduction reprennent cette palette sur leurs fonds, surfaces, champs, boutons et états. Leur montage hors des vues du catalogue exige que leurs variables de couleur soient définies sur les dialogues eux-mêmes. L’aperçu agrandi, ajouté directement au document et partagé par Recherche, Éditions, Archives et Decks (y compris le commandant), reprend le bord naturel de la carte. Les boutons de fermeture des dialogues de carte, de comparaison et de deck reprennent le bouton secondaire de l’en-tête ; celui du deck défile avec son contenu.

## Palette

La visionneuse et son voile sombre apparaissent et disparaissent ensemble sur
130 ms. Le voile conserve sa teinte violette sans flou. Avec la préférence
système de réduction des animations, l’ouverture et la fermeture sont immédiates.
La carte suit les proportions du visuel et reste dans l’espace disponible. Les
coins arrondis et une ombre courte suivent le bord de l’image, sans fond ni
liseré visible dans les zones transparentes. Le même traitement s’applique à
l’aperçu agrandi. En fenêtre étroite ou basse, les actions passent sous l’image.

| Élément | Référence |
| --- | --- |
| Fond de page | `#0e0d15`, uni |
| Barre supérieure | Dégradé de `#1b1728d9` vers `#13121de8`, bord inférieur `#352e43`, flou de fond de 18 px |
| Panneaux | Dégradé de `#231c328c` vers `#191725e6`, touches violette `#7962b025` et bleue `#34487731` cantonnées à la surface |
| Surface secondaire et contours | `#282337` et `#494059` |
| Champs et menus | Champs `#14121e`, menus `#211c30` |
| Notifications et détail de deck | Dégradé de `#282139` vers `#191725`, contours et textes du catalogue. Le chargement et la composition partagent le même fond. |
| Confirmation de fermeture | Reprend le fond violet sombre et les contours du catalogue, avec titre et actions centrés. Annuler utilise le bouton secondaire ; Confirmer utilise le bouton principal. |
| Vignettes en attente ou sans image | Fond graphite violet `#262237`, y compris les couvertures de deck absentes. |
| Titres, texte courant et texte secondaire | `#f6f2ff`, `#f2effb`, `#bdb8cf` |
| Textes indicatifs et surtitres | `#938ba7` et `#bba5ff` |
| Icônes, accent clair et focus | Lilas `#b9a2ff`, texte accentué `#e0d4ff`, contour de focus de la barre `#bfabff` |
| Onglet actif | Dégradé discret de `#3e3255` vers `#302641` et ombre courte, sans trait lumineux |

Les variables historiques `--green` et `--green2` sont lilas dans les vues du catalogue. Réutiliser cette palette pour les nouvelles commandes, y compris les menus montés à la racine du document. Éviter de réintroduire l’ancienne palette verte, jaune ou bronze dans les textes et les icônes d’interface. Le rendu des cartes, les couleurs de mana et les couleurs d’alerte ont leur propre rôle.

## Boutons

Les actions de la visionneuse de carte (recherche d'image, import local,
téléchargement sur le Bureau, ajout à un deck personnel et retournement) utilisent des icônes SVG Lucide
de même taille et de même trait, intégrées sans dépendance React.

| Famille | Référence et états |
| --- | --- |
| Principal | Rechercher, Lancer la mise à jour, Récupérer les images. Fond violet `#533785` avec un léger reflet fixe, bord `#775b9f`, texte blanc `#fffaff`. Au survol, fond `#65479a` et bord `#a287c4`, sans effet lumineux externe. |
| Secondaire | Effacer les filtres sert de référence à Télécharger les images manquantes, Rafraîchir, Historique des versions, Fermer et Notifications. Surface sombre avec un reflet léger. Au survol et au focus clavier, fond `#413653`, bord `#86739f`, texte ou icône `#faf5ff`. Historique des versions associe un pictogramme à gauche et un libellé. |
| Pagination et Archives | Conserver le traitement existant de Précédent, Suivant et des actions d’Archives. |
| Désactivé | Surface sombre et texte atténué. Garder l’emprise du bouton et réserver le survol interactif aux actions disponibles. |

- Rayon de **16 px** pour les boutons principaux, secondaires, les sélecteurs personnalisés et les boutons de la barre. Les contrôles spécialisés, comme les symboles de mana, gardent leur forme. L’ombre portée commune `0 2px 3px #08061140` donne un bord net et un relief discret. Dans la navigation, elle accompagne l’onglet actif ou survolé.
- Barre supérieure : Rafraîchir, les commandes de zoom, Fermer et Notifications
  mesurent **46 × 46 px**. Rafraîchir est placé à gauche du bouton moins.
  Le logo a la même hauteur. Les boutons à icône seule ont un nom accessible.
  Les commandes affichent leur nom au survol et au focus. Le compteur de la cloche est masqué à zéro.
- En-têtes de Recherche, Éditions et Decks : le bouton de téléchargement mesure
  **44 px** de haut. Son libellé reste sur une ligne, même sous le titre.
- Centrer la loupe et le texte de Rechercher comme un seul ensemble. Laisser Rechercher et Effacer les filtres à gauche et le statut à droite. En espace étroit, le statut peut passer dessous en conservant son alignement à droite.
- Donner un retour visible au survol et au focus, notamment pour Rafraîchir. Les transitions de couleur de **150 ms** s’appliquent lorsque la préférence de mouvement réduit le permet. Éviter les animations décoratives en boucle.

## Typographie, marges et alignements

- Titres de page en Georgia, avec replis Times New Roman et serif, `clamp(2rem, 4vw, 3.2rem)`, interligne `1.05`. Texte, libellés et commandes en Inter, puis Segoe UI et polices système. Les libellés de filtres sont à `0.9rem`. Les boutons d’en-tête du catalogue et les états secondaires sont à `0.875rem`.
- Les vues principales partent du même conteneur `main`, limité à **1 240 px**, centré, avec **2 rem** de marge intérieure haute. Suivre les gouttières responsives existantes du conteneur. Les en-têtes partagent **0,4 rem** entre le surtitre et le titre et **1,25 rem** sous l’en-tête. Le titre n’ajoute pas de marge verticale.
- Panneaux standards avec **1,2 rem** de marge intérieure et **14 px** de rayon. Les lignes de filtres partagent un espacement de **0,8 rem**. Une ligne fine sépare les filtres principaux du tri dans Recherche et Decks. Les symboles de mana et le sélecteur de mode restent compacts.
- Garder la barre sur une ligne. En dessous de **1 080 px** ou en portrait, les onglets passent aux pictogrammes avec leurs noms accessibles. Les boutons de droite gardent leur taille.
- Réserver l’emprise des compteurs dès l’ouverture d’Accueil. Les états et résultats vides ne doivent pas créer de grandes zones de blanc sous les commandes.
- L’Accueil affiche les decks perso récemment modifiés dans la largeur du
  panneau, sur sept colonnes en fenêtre large, quatre en fenêtre intermédiaire
  et deux en fenêtre étroite. Le zoom n’affecte pas cette grille d’accueil.

## Boîtes de deck

L’Accueil, les decks préconstruits et les decks personnels partagent le
maillage triangulé `deck-box-mesh.json` du modèle source `deck-box.blend`.
Ces deux fichiers, le grain PNG et le WebP de repli sont versionnés dans
`_bibliotheque_mtg/src/python/bibliotheque_mtg/ui/assets/`. Le serveur lit
les ressources d’exécution depuis ce dossier. L’interface dessine le volume
dans Canvas avec un tampon de profondeur. Les vignettes partagent l’image
calculée au repos et redessinent la géométrie pendant les mouvements. Un WebP
frontal du même modèle sert de repli. La texture PNG reprend le grain du
polymère Blender, atténué pour une finition noire satinée. Le couvercle et le corps partagent le même graphite et le même éclairage
selon leurs normales. Le côté droit est plus sombre que la façade sur toute
la hauteur et un reflet doux souligne le biseau avant droit. La paroi se
raccorde au couvercle sur toute sa profondeur. Le joint modélisé combine
un creux sombre et une lèvre éclairée, avec une remontée vers l’arrière. L’ombre suit la projection
de la base.
Le nom complet du deck apparaît sur le couvercle avec les symboles de mana.
Les textes et logos suivent la taille de la boîte. Les préconstruits
affichent aussi leur édition.

La fenêtre montre le rectangle intérieur de l’illustration d’une carte
standard de 531 × 738 pixels, avec une marge de sécurité à l’intérieur du
cadre. La boîte est légèrement inclinée au repos. Le survol la redresse,
l’agrandit discrètement puis adapte ses angles au déplacement du pointeur.
La sortie du pointeur et le glisser-déposer rétablissent la pose de repos.
La préférence de mouvement réduit conserve une vue fixe.

## Vérification d’une retouche

Recharger l’application et contrôler les cinq vues côte à côte à largeur comparable. Vérifier en vue large et en portrait l’alignement des titres, la barre sur une ligne, le téléchargement sur une ligne et les dimensions du rafraîchissement. Comparer les actions au repos, au survol, au focus clavier et lorsqu’elles sont désactivées. Ouvrir le panneau de notifications et un deck, puis vérifier la surface pendant le chargement, la composition et les vignettes sans image. Contrôler Firefox pour les changements de dimensions ou de retour à la ligne et comparer au navigateur intégré. Vérifier les modales si la retouche touche un sélecteur partagé.
