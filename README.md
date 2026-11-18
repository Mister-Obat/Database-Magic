# Bibliothèque Magic locale

Application Windows de consultation d'un catalogue Magic: The Gathering : recherche et filtres, fiches de cartes, images à la demande, decks préconstruits et personnels, exports et historique des versions. Les métadonnées proviennent notamment de Scryfall et les decks préconstruits de MTGJSON.

Le dépôt contient le programme, pas une bibliothèque déjà constituée : catalogue, archives téléchargées, images, decks personnels, configuration et sauvegardes restent locaux. Une connexion Internet est nécessaire pour le premier import et les mises à jour.

## Installation sous Windows

Installer Python 3.11 avec le lanceur `py`, puis, dans PowerShell à la racine du dépôt :

```powershell
py -3.11 -m venv _bibliotheque_mtg\.venv
& .\_bibliotheque_mtg\.venv\Scripts\python.exe -m pip install -r .\_bibliotheque_mtg\src\python\requirements_bibliotheque.txt
Copy-Item .\_bibliotheque_mtg\config.example.json .\_bibliotheque_mtg\config.json
```

Les chemins de la configuration d’exemple sont relatifs au fichier lui-même. Le `config.json` de chaque installation reste local.

Pour construire le catalogue depuis zéro, lancer la commande suivante et attendre le statut `initialized`.

```powershell
.\Initialiser_Bibliotheque_Magic.bat
```

Prévoir 15 Gio libres. À titre indicatif, une archive All Cards récente pesait environ 389 Mio compressés, et le catalogue occupe plusieurs Gio. L’import télécharge les métadonnées et crée les emplacements d’image vides.

Double-cliquer ensuite sur `Lancer_Bibliotheque_Magic.bat` et ouvrir `http://127.0.0.1:8765/` si nécessaire. Pour actualiser les métadonnées, utiliser `Mettre_a_jour_bibliotheque.bat` ou la page Info. Les images sont récupérées à la demande depuis les fiches.

Le catalogue est normalement sous `_bibliotheque_mtg/v3-staging/` ; une installation existante peut utiliser l'ancien chemin `_bibliotheque_mtg/work/v3-staging/`. Le pointeur `current.json` désigne la version active. Voir le [guide d'utilisation](README_Bibliotheque_Magic.md) pour les données à sauvegarder et les options.

## Développement et tests

Le code est sous `_bibliotheque_mtg/src/python/`. Pour installer les outils de test et exécuter la suite depuis ce dossier :

```powershell
& ..\..\.venv\Scripts\python.exe -m pip install "pytest>=9,<10"
& ..\..\.venv\Scripts\python.exe -m pytest -q
```

Tesseract est facultatif pour l'OCR et Codex CLI pour les fonctions qui l'utilisent ; ils ne sont pas nécessaires à la recherche ordinaire. Voir le guide pour la configuration OCR.
