# Builds pour l'App Store et Google Play

Les builds se font sur les serveurs de GitHub avec le workflow **Stores MOCAM**
(`.github/workflows/stores.yml`). Vos clés restent dans les *secrets* GitHub : elles ne sont
jamais écrites dans le dépôt.

À préparer une seule fois, puis : **Actions › Stores MOCAM › Run workflow**.

---

## 1. Android (Google Play)

### Créer la clé d'envoi (une fois, sur votre PC)

Il faut Java (fourni avec Android Studio). Dans PowerShell :

```powershell
keytool -genkeypair -v -storetype PKCS12 -keystore mocam-upload.p12 -alias mocam `
  -keyalg RSA -keysize 2048 -validity 10000
```

Choisissez un mot de passe et répondez aux questions (nom, pays…).
**Gardez `mocam-upload.p12` et son mot de passe en lieu sûr** (sauvegarde hors du PC) :
sans eux, vous ne pourrez plus envoyer de mise à jour sans passer par le support Google.

Encodez-la pour GitHub :

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("mocam-upload.p12")) | Set-Clipboard
```

(le texte est copié dans le presse-papiers).

### Secrets GitHub

Sur github.com/BaptisteTTD/MOCAM › **Settings › Secrets and variables › Actions › New repository secret** :

| Nom | Valeur |
|---|---|
| `ANDROID_KEYSTORE_B64` | le texte copié ci-dessus |
| `ANDROID_KEYSTORE_PASSWORD` | le mot de passe choisi |
| `ANDROID_KEY_ALIAS` | `mocam` |

### Envoi dans Play Console

1. Lancez le workflow (plateforme *android* ou *les deux*).
2. À la fin, ouvrez l'exécution › **Artifacts › MOCAM-play** : téléchargez et dézippez → `MOCAM-play.aab`.
3. Play Console › MOCAM › **Tests › Test interne** (ou **Production**) › **Créer une release** ›
   déposez `MOCAM-play.aab`. Acceptez *Signature d'application Play* quand c'est proposé
   (Google garde la clé finale, la vôtre ne sert qu'à envoyer).

---

## 2. iPhone (App Store)

Le build est signé et **envoyé directement dans App Store Connect** : pas de .ipa à manipuler.

### Prérequis

- L'app existe dans App Store Connect avec l'identifiant de lot `com.mocam.app`
  (créé dans developer.apple.com › Identifiers si la liste ne le propose pas).

### Clé API App Store Connect (une fois)

1. App Store Connect › **Utilisateurs et accès › Intégrations › Clés de l'API App Store Connect**
   › onglet *Clés d'équipe* › **+**.
2. Nom : `GitHub MOCAM`, accès : **Admin** (nécessaire pour qu'Apple crée lui-même le certificat
   de distribution et le profil).
3. Notez l'**ID de la clé** et l'**ID de l'émetteur** (affiché au-dessus du tableau),
   puis **Télécharger** la clé : fichier `AuthKey_XXXXXXXXXX.p8` (téléchargeable une seule fois).

Votre **Team ID** : developer.apple.com › Account › **Membership details** (10 caractères).

### Secrets GitHub

| Nom | Valeur |
|---|---|
| `APPLE_TEAM_ID` | le Team ID |
| `ASC_KEY_ID` | l'ID de la clé |
| `ASC_ISSUER_ID` | l'ID de l'émetteur |
| `ASC_KEY_P8` | tout le contenu du fichier `.p8` (ouvert avec le Bloc-notes, lignes BEGIN/END comprises) |

### Après le build

1. Lancez le workflow (plateforme *iphone* ou *les deux*) : environ 20 à 30 minutes.
2. Le build apparaît dans App Store Connect › MOCAM › **TestFlight** après 5 à 30 minutes de
   traitement par Apple. La question sur le chiffrement est déjà
   réglée dans l'app (aucun chiffrement propriétaire).
3. Onglet **Distribution** › version 1.9.0 › section *Build* › **+** › choisissez le build ›
   **Ajouter pour vérification**.

---

## Numéros de build

Le workflow les fixe tout seul à `1000 + numéro de l'exécution` : chaque envoi est plus grand
que le précédent, comme l'exigent les deux stores. La version affichée (1.9.0) vient de
`mobile/MocapCamPhone/app.json` : changez-la là pour publier une nouvelle version.

## En cas d'échec

Ouvrez l'exécution dans **Actions** : l'étape en rouge affiche l'erreur. Les messages
« Secrets … manquants » indiquent qu'un secret n'est pas créé ou mal nommé.
