# Stack, configuration et commandes

## Technologies constatées

- Windows et lanceurs `.bat` ; environnement isolé `_bibliotheque_mtg/.venv` en
  Python **3.11.8** sur ce poste.
- Versions observées dans cet environnement le 21 octobre 2026 : `requests 2.34.2`,
  `beautifulsoup4 4.15.0`, `Pillow 13.0.0` et `jsonschema 4.26.0`. Les versions
  attendues sont figées dans le fichier de dépendances listé ci-dessous.
- `sqlite3` standard avec tables relationnelles, contraintes de clé étrangère et
  index de recherche **FTS5** (`v2/schema.sql`).
- `http.server.ThreadingHTTPServer` pour l’application locale ; interface HTML,
  CSS et JavaScript sans étape de compilation détectée.
- `requests` pour les fournisseurs, `beautifulsoup4` pour les galeries, Pillow
  pour le décodage et le WebP, `jsonschema` pour les contrats de l’ancien
  pipeline.
- Versions directes et transitives figées dans
  `_bibliotheque_mtg/src/python/requirements_bibliotheque.txt`.

## Configuration effective

- `_bibliotheque_mtg/config.json` définit les chemins, le périmètre, les priorités
  de sources, les seuils d’image et les limites réseau du pipeline.
- Les chemins dans `config.example.json` sont relatifs au fichier de configuration.
  Les chemins absolus des configurations existantes restent pris en charge.
- Le serveur local dérive ses chemins depuis `local_app.py` et utilise
  `MAGIC_LIBRARY_PORT` pour le port. Le téléchargeur V2 a aussi des constantes
  internes (`_MAX_GETS=3`, `_MAX_BYTES=20 MiB`, résolution cible 531×738).
- `MAGIC_LIBRARY_CONFIG` permet au lanceur de mise à jour d’utiliser un autre
  fichier de configuration.
- `v3-staging/current.json` désigne la version active ; les fichiers de
  chaque version sont sous `versions/<version_id>/`.

## Commandes vérifiées le 21 octobre 2026

Depuis la racine du projet, l’aperçu réel a retourné le code 0, zéro appel réseau
et zéro écriture publiée :

```powershell
& '.\Mettre_a_jour_bibliotheque.bat' --dry-run
```

Le démarrage utilisateur se fait avec `Lancer_Bibliotheque_Magic.bat`. La
page observée répond sur `http://127.0.0.1:8765/` et `/api/v2/stats` renvoie les
compteurs du snapshot actif.

Pour les commandes Python, **se placer dans le dossier qui contient
`bibliotheque_mtg/`** :

```powershell
$projet = (Get-Location).Path
Set-Location "$projet\_bibliotheque_mtg\src\python"
& "$projet\_bibliotheque_mtg\.venv\Scripts\python.exe" -m bibliotheque_mtg --help
& "$projet\_bibliotheque_mtg\.venv\Scripts\python.exe" -m bibliotheque_mtg --config "$projet\_bibliotheque_mtg\config.json" update --dry-run
```

`--help` a retourné le code 0 et les commandes `sources`, `validate`, `audit`,
`reconcile`, `publish`, `deploy`, `report`, `init`, `activate`, `update`, `backup`, `restore`,
`collect`. Lancer le module depuis `Magic/` sans adapter `PYTHONPATH` échoue avec
`No module named bibliotheque_mtg` ; voir [audit](audit.md).

## Tests déjà enregistrés

Le [rapport final](../_bibliotheque_mtg/reports/validation_v2.md) consigne 13
tests `test_regles`, 73 tests historiques et 40 tests V2 réussis, ainsi que
`py_compile` et `node --check`. La commande de suite V2 est
`python -m pytest -q tests_v2` depuis `_bibliotheque_mtg/src/python` avec un Python
doté de `pytest` ; l’environnement applicatif isolé ne contient pas `pytest`.

Les commandes d’entretien et de restauration sont décrites dans le
[workflow dédié](workflows/sauvegarde-restauration.md). Aucun téléchargement
massif d’images n’est déclenché par `update`.
