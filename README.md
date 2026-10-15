# Bus et trams en direct — Montpellier

Petite application web pour visualiser les positions en direct des bus et des trams TAM. Elle utilise les informations de ligne et de destination du GTFS statique, et affiche les tracés colorés des lignes de tramway, de bus et du Bustram A. Les arrêts de bus et de tram donnent accès aux prochains horaires théoriques et aux prédictions temps réel. Quand le GTFS n’indique pas la direction d’un tram, l’application l’estime à partir de son cap et des tracés locaux ; si les données ne suffisent pas, elle affiche « Direction inconnue ».

## Démarrage

Prérequis : Python 3.10 ou plus récent et une connexion Internet pour les positions TAM, le GTFS, Leaflet et les tuiles OpenStreetMap. Les tracés des trams, des bus et du Bustram A sont fournis localement avec le projet.

Dans un terminal ouvert dans ce dossier :

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Ouvre ensuite <http://127.0.0.1:5000> dans ton navigateur. Pour arrêter le serveur, fais `Ctrl+C` dans le terminal.

La page actualise les positions et les horaires temps réel toutes les 15 secondes. Les marqueurs glissent vers leur nouvelle position en 2 secondes ; ceux des trams suivent le tracé le plus proche quand la géométrie est disponible. Les bus et les trams sont regroupés par mode de transport ; clique sur une ligne pour filtrer les véhicules. Les quais d’un même arrêt sont regroupés afin d’afficher les horaires des deux directions au même endroit. Clique sur un arrêt de la carte pour ouvrir ses horaires. Si Windows bloque l’activation de l’environnement virtuel PowerShell, lance les commandes suivantes à la place :

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

## Liens vers les fiches TC Infos

Render bloque l’accès de son serveur aux pages de recherche TC Infos (HTTP 403). L’application utilise donc un index local de numéros de parc vers fiches véhicules. Pour le générer ou le mettre à jour depuis un ordinateur qui accède à TC Infos, lance à la racine du projet :

```powershell
.venv\Scripts\python.exe tools\update_tc_infos_index.py
```

Le script crée `data/tc_infos_vehicles.json`. Il inclut les numéros principaux des parcs TaM et vérifie aussi les numéros des véhicules actuellement en circulation, car TC Infos peut lister plusieurs anciens numéros pour une même fiche. Ajoute ce fichier au dépôt avec `app.py` et `tools/update_tc_infos_index.py`, puis redéploie sur Render. Le site utilisera l’index sans interroger TC Infos depuis Render. Si le parc évolue ou si un numéro ne trouve pas sa fiche, relance le script et redéploie le nouvel index. Si l’adresse de ton site Render change, règle la variable `TAM_LIVE_API_URL` sur son URL `/api/vehicles` avant de lancer le script.

## Sources

- Positions : [VehiclePosition GTFS-Realtime TAM](https://gtfsproxy.e-tam.fr/COMMON/VehiclePosition.pb)
- Prédictions d’horaires : [TripUpdate GTFS-Realtime TAM](https://gtfsproxy.e-tam.fr/COMMON/TripUpdate.pb)
- Lignes, couleurs et destinations : [GTFS TAM](https://gtfsproxy.e-tam.fr/COMMON/GTFS.zip)
- Arrêts de tram : [MMM_MMM_ArretsTram.json](https://data.montpellier3m.fr/sites/default/files/ressources/MMM_MMM_ArretsTram.json)
- Arrêts de bus : [MMM_MMM_ArretsBus.json](https://data.montpellier3m.fr/sites/default/files/ressources/MMM_MMM_ArretsBus.json)
- Tracés de tramway : copie locale de [MMM_MMM_LigneTram.json](https://data.montpellier3m.fr/sites/default/files/ressources/MMM_MMM_LigneTram.json), dans `data/`
- Tracés de bus : fichier local `data/MMM_MMM_BusLigne.json` fourni par l’utilisateur
- Tracé du Bustram A : fichier local `data/MMM_MMM_Bustram.json` fourni par l’utilisateur
- Fond de carte : [OpenStreetMap](https://www.openstreetmap.org/copyright)

Les URL des flux peuvent être remplacées par les variables d’environnement `TAM_VEHICLE_POSITIONS_URL`, `TAM_TRIP_UPDATES_URL`, `TAM_GTFS_URL`, `TAM_TRAM_STOPS_URL` et `TAM_BUS_STOPS_URL`. Les chemins des GeoJSON locaux peuvent être remplacés avec `TAM_TRAM_LINES_FILE`, `TAM_BUS_LINES_FILE` et `TAM_BUSTRAM_LINES_FILE`. Le port local peut être modifié avec `PORT`.
