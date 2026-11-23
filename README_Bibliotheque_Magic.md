# Guide de la Bibliothèque Magic

## Première utilisation

Suivre l'installation et l'initialisation du [README](README.md), puis lancer `Lancer_Bibliotheque_Magic.bat`. L'interface est accessible sur `http://127.0.0.1:8765/`. L'initialisation nécessite Internet, plusieurs Gio d'espace et du temps ; prévoir 15 Gio libres. L'archive All Cards observée atteignait environ 389 Mio compressés, une taille susceptible de changer.

Utiliser la recherche et ses filtres pour trouver une carte et ouvrir sa fiche. Choisir une impression et une face pour télécharger une image ; l'application privilégie le français et indique la langue retenue. « Vérifier ou réparer » permet de traiter une image endommagée. La présence d'une image et la disponibilité des métadonnées sont deux états distincts.

## Actualisation et versions

`Mettre_a_jour_bibliotheque.bat` actualise les métadonnées ; la page Info propose aussi une mise à jour et l'historique des versions. La commande suivante prévisualise la mise à jour sans accès réseau :

```powershell
.\Mettre_a_jour_bibliotheque.bat --dry-run
```

L'actualisation ne télécharge pas les images en masse. Le catalogue publié est normalement sous `_bibliotheque_mtg/v3-staging/` ; les installations existantes peuvent encore utiliser `_bibliotheque_mtg/work/v3-staging/`. `current.json` désigne la version active. La publication d'une nouvelle version remplace ce pointeur après construction ; une restauration de sauvegarde ne l'active pas automatiquement.

## Decks et exports

L'onglet **Decks** permet de consulter les préconstruits issus de MTGJSON. L'onglet **Perso** permet de créer et modifier des compositions, ou de copier un préconstruit. Depuis une fiche, le bouton **+** ajoute l'impression à un deck personnel. Attendre l'indication **Enregistré** avant de quitter ; un brouillon peut rester dans le navigateur en cas d'erreur.

Les decks peuvent être exportés en images sur le Bureau. Les images nécessaires doivent être disponibles : récupérer les cartes manquantes depuis leurs fiches. La page Info accepte aussi l'import d'une liste de cartes.

Pour personnaliser une finition panoramique, remplacez son fichier
`deck-box-<identifiant>.webp` dans
`_bibliotheque_mtg/src/python/bibliotheque_mtg/ui/assets/` en gardant le même
nom et un panorama au format 2:1. Relancez la bibliothèque avec son lanceur
habituel. Elle régénère l’aperçu fixe avec le modèle 3D si le contenu de la
texture a changé. Le rendu utilise une installation locale de Chrome ou d’Edge.
Si le rendu échoue, l’ancien aperçu est conservé et la bibliothèque s’ouvre.
Pour ajouter une finition au sélecteur, déclarez son identifiant dans
`PANORAMIC_BOX_DESIGNS` dans `personal_decks.py`, puis ajoutez son libellé dans
le groupe voulu dans `ui/deck-box.js`. Le prochain démarrage crée son aperçu.

## Données locales et sauvegarde

Le dépôt ne sauvegarde pas les données produites sur votre machine. Pour les protéger, conserver une copie externe du catalogue et de ses sauvegardes, de `_bibliotheque_mtg/Images/`, de `_bibliotheque_mtg/state/` (notamment `personal-decks.sqlite` et `personal-decks-backups/`), des archives et de tout fonds historique personnel. La sauvegarde du catalogue ne comprend pas les images : copier `Images/` séparément. Éviter les mises à jour et téléchargements d'images pendant la sauvegarde.

Depuis la racine du projet, ces commandes créent une sauvegarde datée et permettent sa restauration. Arrêter l’application avant la restauration et l’activation.

```powershell
$ops = (Resolve-Path .\_bibliotheque_mtg).Path
$python = Join-Path $ops '.venv\Scripts\python.exe'
$config = Join-Path $ops 'config.json'
$copie = Join-Path $ops ("backups\catalogue-" + (Get-Date -Format yyyyMMdd-HHmmss))
Push-Location (Join-Path $ops 'src\python')
& $python -m bibliotheque_mtg --config $config backup --destination $copie
Pop-Location
```

Sur une **nouvelle installation**, mettre `$copie` au chemin de la sauvegarde transférée, puis lancer les commandes suivantes depuis la racine. Sur une installation existante, indiquer comme destination la racine du catalogue déjà actif.

```powershell
Push-Location (Join-Path $ops 'src\python')
& $python -m bibliotheque_mtg --config $config restore --backup $copie --destination-root (Join-Path $ops 'v3-staging')
# Reprendre la valeur version_id affichée par restore après avoir vérifié la copie.
& $python -m bibliotheque_mtg --config $config activate --version 'v2-ID-DE-LA-VERSION'
Pop-Location
```

Restaurer aussi la copie de `Images/` et celle de l’état personnel, puis relancer l’application. Vérifier la recherche et un échantillon des images avant de libérer la copie d’origine.

## Options

L'OCR nécessite Tesseract, facultatif. Le programme peut le trouver via `MAGIC_LIBRARY_TESSERACT`, le `PATH` ou son emplacement Windows habituel. Les modèles français et anglais sont fournis dans [tools/tessdata](_bibliotheque_mtg/tools/tessdata/README.md). Codex CLI est également facultatif pour les fonctions qui l'utilisent. `MAGIC_LIBRARY_CONFIG` permet de choisir un autre fichier de configuration dans `_bibliotheque_mtg/`.
