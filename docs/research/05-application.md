# Étape 5 — Une application à soi sur l'iPhone

> Objectif : remplacer Telegram par une application Orion sur l'iPhone du propriétaire, pour
> parler à Orion, approuver ses actions et l'administrer, sans payer le programme développeur
> d'Apple (99 $ par an) et sans rien d'utilisable par quelqu'un d'autre.
>
> Date de l'étude : 26 octobre 2026. Ce qui touche à iOS vient de la documentation d'Apple et du
> WebKit ; rien n'a encore été essayé sur un vrai iPhone.

---

## 0. En une page

1. **Une PWA, pas une appli native.** Sans le programme payant, une appli native n'a pas de
   notifications (APNs est réservé aux comptes payants) et doit être re-signée tous les 7 jours.
   Une application web ajoutée à l'écran d'accueil reçoit de vraies notifications (Web Push,
   iOS 16.4 et plus), n'expire pas, s'ouvre en plein écran, et Face ID passe par les passkeys.
2. **Elle n'est pas « sur le web » plus qu'une appli native.** Les deux parlent au serveur par
   Internet, à la même adresse. Ce qui protège, c'est l'appairage : sans une passkey créée sur
   son iPhone à partir d'un code à usage unique, l'adresse ne sert à rien.
3. **La page est servie par le gardien, la conversation par le noyau.** Une demande d'accord
   doit être écrite et vérifiée hors du noyau (D28, D89) : si le noyau servait la page, un noyau
   compromis pourrait afficher « Envoyer le mail à Paul » et faire signer autre chose. Le gardien
   sert donc les fichiers de l'application, rédige les questions et vérifie les signatures.
4. **Un oui est une signature.** Chaque demande porte un défi tiré par le gardien ; l'iPhone le
   signe avec la passkey après Face ID. Le noyau transmet la réponse, il ne peut pas la fabriquer.
   C'est plus fort que Telegram, où un 👍 suffit.
5. **Les notifications passent par le gardien.** La clé VAPID est un secret : elle vit chez le
   gardien, qui chiffre chaque notification pour l'abonnement du téléphone (RFC 8291). Apple ne
   voit que du chiffré.
6. **Railway** convient au mode réuni (un seul processus, sans gardien séparé), pas au mode
   séparé, qui demande deux utilisateurs Unix et une socket partagée.

---

## 1. Pourquoi pas une appli native

| | Native, identifiant Apple gratuit | PWA sur l'écran d'accueil |
|---|---|---|
| Notifications | non (APNs payant) ; contournement par ntfy | **Web Push** (iOS 16.4+) |
| Durée | 7 jours, re-signée par SideStore ou AltStore | **sans limite** |
| Installation | compilée ailleurs (Mac ou GitHub Actions), installée par SideStore | ouvrir l'adresse, « Sur l'écran d'accueil » |
| Face ID | Secure Enclave | **passkeys (WebAuthn)** |
| Partage depuis Photos ou Safari | extension, limitée sans compte payant | non (pas de Web Share Target sur iOS) |
| Widgets, Live Activity | limités sans compte payant | non |

Ce qu'on perd : le partage direct depuis d'autres applis, les widgets. On envoie une photo par
le bouton « + » de l'application, comme dans n'importe quelle messagerie.

Contraintes de la PWA à respecter :
- HTTPS et un nom de domaine (une passkey est liée à un domaine, jamais à une adresse IP ; un
  service worker exige HTTPS). Le domaine de la console (`public_url`) convient ;
- les notifications ne marchent qu'une fois l'application ajoutée à l'écran d'accueil, et
  après un geste du propriétaire (un bouton « Activer les notifications ») ;
- le stockage d'une application de l'écran d'accueil n'est pas purgé au bout de 7 jours comme
  celui de Safari, mais iOS peut le vider sous pression : l'application doit pouvoir repartir
  de zéro (rejouer l'appairage), et rien d'important ne vit que sur le téléphone.

---

## 2. Qui sert quoi

```
iPhone (PWA)                Caddy (HTTPS, domaine)          serveur
  page, service worker  ─→  /app/            ─→  gardien   (fichiers figés de l'application)
  appairage, passkey    ─→  /app/pair, /app/key  gardien   (code, enregistrement, vérification)
  demandes d'accord     ─→  /app/approvals   ─→  gardien   (texte rédigé par lui, défi, signature)
  conversation          ─→  /app/api/        ─→  noyau     (messages, fichiers, flux)
  notifications         ←─  Web Push (Apple) ←─  gardien   (clé VAPID, chiffrement)
```

- **Même origine** : un seul domaine, des chemins différents. La passkey vaut pour tout le
  domaine, et aucune requête n'est inter-origines.
- **Les fichiers de l'application** (HTML, JS, CSS, icônes, manifeste, service worker) sont
  livrés dans le paquet Python et servis tels quels par le gardien, avec une politique de
  sécurité du contenu stricte (aucun script tiers, aucun script en ligne). Le noyau ne peut pas
  les modifier.
- **En mode réuni** (sans gardien séparé, comme sur Railway), le même processus sert tout : on
  garde le même découpage dans le code, on perd la frontière entre processus. C'est le niveau
  de Telegram sans gardien aujourd'hui.

## 3. L'appairage

1. Sur le serveur : `orion app pair iphone` (sous l'utilisateur du gardien en mode séparé :
   `orion-admin app pair iphone`). Le gardien tire un code à usage unique (10 minutes) et
   affiche l'adresse `https://<domaine>/app/pair` et un QR code.
2. Sur l'iPhone : on ouvre l'adresse dans Safari, on l'ajoute à l'écran d'accueil, on l'ouvre,
   on saisit le code. L'application crée une passkey liée à l'appareil (`userVerification:
   required`, donc Face ID ou le code du téléphone).
3. Le gardien vérifie l'enregistrement (défi, origine, domaine), garde la clé publique dans son
   registre, et ouvre une session : un cookie HttpOnly, Secure, SameSite=Strict, limité à
   `/app/`, signé par le gardien.
4. Le noyau apprend qu'un appareil est appairé (son nom, jamais la clé) et accepte ses requêtes
   sur présentation d'un jeton de session que le gardien atteste.

Un appareil se retire par `orion app revoke iphone`. Un deuxième appareil (un iPad) refait
l'appairage. Une session expire ; une passkey se rejoue avec Face ID pour en rouvrir une, sans
nouveau code.

## 4. Les accords

1. Le noyau demande l'accord au gardien, comme aujourd'hui (`request_approval`), avec la
   conversation `app:<appareil>`.
2. Le gardien rédige la question (`renderers`, comme pour Telegram), tire un défi de 32 octets,
   et enregistre la demande (`APPROVAL_REQUESTED`). Il envoie une notification.
3. L'application lit la question **chez le gardien** (`/app/approvals`), l'affiche, et à « Oui »
   ou « Non » demande une assertion WebAuthn sur le défi. Le défi est lié à la demande et au
   choix (`approval_id`, `oui`/`non`) : une signature de « non » ne peut pas servir de « oui ».
4. Le gardien vérifie la signature (clé publique enregistrée, compteur, origine, `UV`), et note
   la réponse dans son registre. `_owner_said_yes` lit ce registre.

Sans réponse avant l'expiration, c'est non, comme partout. Un accord vaut pour cette action
seulement.

## 5. Les notifications

- À l'activation, l'application s'abonne (`PushManager.subscribe`, avec la clé publique VAPID du
  gardien) et transmet l'abonnement au gardien, qui le garde.
- Le noyau demande au gardien d'envoyer une notification au propriétaire (texte court, et de
  quoi ouvrir la bonne conversation). Le gardien ne connaît que les abonnements des appareils
  appairés, chiffre le contenu (aes128gcm), signe la requête (VAPID), et limite le débit, comme
  pour Telegram (`max_sends_per_minute`).
- Le texte complet d'un message ne voyage qu'au service worker, chiffré ; la notification en
  montre le début. L'ouverture de l'application recharge la conversation chez le noyau.

## 6. La conversation

L'application est un canal comme un autre (D88) : un module `app` qui dépose les messages dans
la boîte et lui fournit une sortie (`Outlet`, `FileOutlet`, `AskingOutlet`).

- **Envoyer** : texte, photos et fichiers (téléversés au noyau, puis vus par le module photos
  comme ceux de Telegram), messages vocaux (enregistrés par le navigateur, transcrits comme les
  vocaux Telegram).
- **Recevoir** : un flux (Server-Sent Events) pendant que l'application est ouverte : réponses,
  messages d'attente, « en train d'écrire », messages d'initiative, fichiers. Fermée, elle
  reçoit une notification ; rouverte, elle rattrape ce qu'elle a manqué depuis son dernier
  message lu (le journal est la source, l'application n'est qu'une vue).
- **Réactions** : un appui long sur un message (👍, 👎, ❤️), qui deviennent des
  `feedback.reaction` comme sur Telegram.
- L'outbox du canal journalise chaque envoi ; un message qui n'a pas pu partir est rejoué.

## 7. L'administration

Tout ce que fait la console (D101) en écrans de l'application, avec les mêmes événements :
dépenses, ce qui est prévu, missions et tâches de fond, mémoire (voir, oublier), engagements,
manuel, autonomie, réglages et modules (par `configure_change`, donc avec un accord signé),
identifiants enregistrés (noms seulement), santé du serveur (versions, mise à jour, redémarrage
demandé au gardien). Les secrets se saisissent dans l'application et sont chiffrés pour le
gardien, comme le formulaire de D94.

## 8. Railway

Railway lance un conteneur par service, avec un volume et une adresse HTTPS publique.
- **Mode réuni** : un service, `orion serve`, un volume pour `data/`, les secrets en variables
  d'environnement. L'application, l'appairage et les accords marchent, sans frontière entre
  noyau et gardien.
- **Mode séparé** : il faut deux processus sous deux utilisateurs avec une socket partagée,
  donc un seul conteneur qui les lance tous les deux (un superviseur), ou rester sur le
  serveur actuel (`deploy/`). Deux services Railway ne partagent pas de socket Unix.
- Le domaine fourni par Railway (`*.up.railway.app`) suffit pour les passkeys, mais changer de
  domaine plus tard oblige à refaire l'appairage : mieux vaut un domaine à soi dès le début.

## 9. Ordre de construction

1. Le canal côté serveur, en mode réuni : appairage, passkey, session, conversation, flux,
   fichiers, réactions, accords signés, notifications ; tests. Le mode séparé refuse de
   charger le canal tant que l'étape 2 n'est pas faite (comme Telegram avant D89).
2. Le gardien : servir l'application, appairage, accords signés et notifications chez lui.
3. L'interface : plusieurs directions proposées au propriétaire, puis l'application.
4. L'administration, écran par écran.
