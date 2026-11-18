# Protocole MocapCam Studio <-> téléphones

Transport : WebSocket `ws://<pc>:<port>/ws/phone` + HTTP pour l'envoi des vidéos.

## Connexion
Le PC affiche un QR code contenant du JSON :
`{"mocapcam":1,"hosts":["192.168.1.20","10.0.0.5"],"port":8765,"token":"ab12cd"}`.
C'est le seul moyen de connexion (pas de saisie d'adresse). Le téléphone ouvre une connexion
vers toutes les adresses en même temps (délai 4 s) et garde la première qui répond, puis envoie :
`{"t":"hello","token":"ab12cd","name":"iphone","platform":"ios","model":"...","app":"1.1.0"}`
Le PC répond `{"t":"welcome","id":"c1","name":"iphone","fov":1}` (`fov` : dernier champ de vision
choisi pour ce nom de caméra, absent s'il n'y en a pas). Un partage de connexion de téléphone
est un réseau comme un autre : le PC y a une adresse, qui figure dans `hosts`.

## Horloge
Le PC mesure le décalage de chaque téléphone :
PC -> `{"t":"ping","s0":<ms PC>}` ; téléphone -> `{"t":"pong","s0":…,"c":<Date.now() téléphone>}`.
Décalage = c − (s0 + s1)/2, on garde l'échange au plus petit aller-retour.

## Commandes PC -> téléphone
| message | effet |
|---|---|
| `{"t":"arm","take":"prise_001"}` | prépare l'enregistreur |
| `{"t":"start","take":"prise_001","at":<ms téléphone>}` | démarre à l'heure dite |
| `{"t":"stop"}` | arrête, puis envoie la vidéo |
| `{"t":"lock","fast_shutter":true}` | bloque exposition/mise au point/balance des blancs |
| `{"t":"unlock"}` | repasse en automatique |
| `{"t":"snapshot"}` | image pleine résolution (contrôle des boules) ; le PC la redemande jusqu'à 3 fois (6 s) sans réponse |
| `{"t":"identify"}` | affiche le nom en grand quelques secondes |
| `{"t":"rename","name":"iphone"}` | change le nom |
| `{"t":"preview","fps":3}` | cadence de l'aperçu (0 = coupé) |
| `{"t":"config","fov":0.5,"lens":"ultra"}` | champ de vision en équivalent zoom de l'objectif ×1 (0,5 = ultra grand-angle … 2) ; `lens` pour les apps < 1.1 |

## Messages téléphone -> PC
- `{"t":"status", ...}` chaque seconde : batterie, stockage, i/s et format réellement obtenus, exposition, état,
  `fov` (champ de vision actuel), `fov_options` (valeurs possibles sur ce téléphone), `app` (version).
  Un changement de `fov` fait sur le téléphone est retenu par le PC pour ce nom de caméra.
- `{"t":"recording","take","called_at","started_at"}` (ms téléphone).
- `{"t":"recorded","take","duration","size"}` puis envoi HTTP `POST /upload?token=…&cam=…&take=…`
  (corps = fichier brut) avec `{"t":"upload","take","sent","total"}` pendant l'envoi.
- Binaire : 1er octet = 1 (aperçu) ou 2 (image pleine résolution), puis le JPEG.
