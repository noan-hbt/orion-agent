# Déployer Orion sur un serveur (VPS OVH ou autre)

Orion tourne en deux processus (D28), sous deux utilisateurs Unix qui partagent un groupe :

- `orion-guardian` détient les secrets (Inkbox, agendas, mail, recherche web, clé de
  fédération) et vérifie chaque effet sur le monde ;
- `orion-core` ne détient que la clé du modèle (OpenRouter).

Caddy, devant eux, sert le HTTPS et laisse passer seulement le webhook Inkbox (vers le cœur)
et la fédération (vers le gardien). Tout le reste répond 404.

## Installation

Sur un VPS neuf sous Debian 12 ou Ubuntu 24.04, connecté en SSH :

```bash
git clone https://github.com/noan-hbt/orion-agent.git && cd orion-agent
sudo deploy/install.sh orion.mondomaine.fr     # ou sans argument : <ip>.sslip.io
```

Pour un nom de domaine à toi, crée d'abord un enregistrement A vers l'IP du VPS (dans OVH :
Web Cloud > Domaines > Zone DNS). Sans domaine, le script utilise `sslip.io`, qui suffit
pour obtenir un certificat.

Le script installe tout (Caddy, uv, Python, le navigateur d'Orion, les utilisateurs, les
services, le pare-feu), puis pose quelques questions (`orion setup`) :

1. ton prénom et ton fuseau horaire ;
2. la clé OpenRouter (vérifiée tout de suite) ;
3. Telegram : crée un bot avec @BotFather (`/newbot`) et colle son jeton (vérifié), puis
   écris au bot depuis ton téléphone : Orion lit ton id dans ce message, tu n'as rien à
   recopier. Le bot te répond pour confirmer.

Les secrets vont dans `/etc/orion/core.env` (la clé du modèle, `orion-core`) et
`/etc/orion/guardian.env` (le reste, `orion-guardian`), en 0600, jamais dans `orion.toml`.
Puis Orion démarre.

**Ensuite, plus besoin de toucher aux fichiers** : dis-lui « active le mail », « ajoute
Angèle, elle peut savoir quand je suis libre », « mets tes points à 9 h ». Il change ses
réglages lui-même (`/srv/orion/config/orion.toml`), avec ton accord. Ce qui te protège (le
gardien, les canaux, qui peut lui écrire, le budget) n'est changé qu'avec ton oui au
changement exact, vérifié par le gardien, qui est le seul à pouvoir écrire ce fichier (D92).
Les secrets ne passent jamais par message : pour en ajouter un (le mot de passe d'une boîte
mail…), mets-le dans `/etc/orion/guardian.env` et redémarre.

Relancer le script met le code à jour ; il ne touche jamais à la configuration ni aux
secrets, et ne repose les questions que si la clé OpenRouter manque.

iMessage (par Inkbox, payant) au lieu de Telegram : mets `[channels.imessage]` `enabled =
true`, ton numéro et le handle d'Orion, `INKBOX_API_KEY` dans `guardian.env`, puis :

```bash
sudo /srv/orion/app/deploy/orion-admin inkbox setup \
    --webhook-url https://orion.mondomaine.fr/inkbox/webhook
```

Mets la clé de signature qu'il affiche dans `INKBOX_SIGNING_KEY` (`guardian.env`), envoie
depuis ton téléphone la commande de connexion qu'il affiche, et redémarre :
`sudo systemctl restart orion-guardian orion-core`.

`orion-admin` lance une commande `orion` sous l'utilisateur du gardien, avec ses secrets
(`federation invite`, `inkbox calls`…). Avec `--core`, il la lance sous celui du cœur
(`doctor`, `memory`, `chat`).

## Au quotidien

- **Logs** : `journalctl -u orion-core -f`, `journalctl -u orion-guardian -f`.
- **Si le cœur tombe** : systemd le relance après 5 s. S'il reste muet plus de 5 minutes, le
  gardien t'écrit lui-même par iMessage (le cœur ne peut pas te prévenir qu'il est arrêté),
  puis encore quand il repart.
- **Sauvegardes** : chaque nuit à 4 h 15, une copie vérifiée du journal du cœur et du registre
  du gardien va dans `/srv/orion/backups/` (30 copies gardées). Tout le reste (mémoire,
  personnes, rappels, fiches de mail) se reconstruit à partir du journal.
  - Pour une copie hors du serveur, avec rclone vers OVH Object Storage par exemple :
    `rclone sync /srv/orion/backups ovh:orion-backups` dans un timer à toi.
  - Le journal contient ta vie : chiffre le bucket.
- **Mise à jour** : automatique (D94). Toutes les 2 minutes, `orion-update.timer` regarde
  si la branche suivie a du nouveau. Si oui, il installe, vérifie la config, redémarre et
  surveille les deux processus 75 s. Si quelque chose casse, il revient à la version d'avant,
  écarte ce commit et te prévient sur Telegram ; il te prévient aussi quand ça s'est bien passé.
  Journal : `journalctl -u orion-update`. Le couper : `sudo systemctl disable --now
  orion-update.timer`. Changer de branche : `sudo git -C /srv/orion/app checkout <branche>`.
- **Secrets depuis le téléphone** (D94) : « active mon mail » → Orion t'envoie sur Telegram un
  bouton qui ouvre le formulaire du gardien (`/secrets/`). Tu y colles le mot de passe ; il va
  directement au gardien (`/srv/orion/guardian/secrets.env`), jamais dans la conversation.
- **Restaurer** : arrête les deux services, copie `journal.sqlite3` d'une sauvegarde dans
  `/srv/orion/data/`, supprime `memory.sqlite3` (reconstruit au démarrage), redémarre.

## Agir sur tes comptes

Comme le navigateur de ChatGPT (D116) : Orion a un seul navigateur, qui garde ses connexions
(sur le serveur, même après un redémarrage). Quand une page a besoin de toi (te connecter, un
code reçu par SMS, un captcha), il t'envoie sur Telegram un bouton qui ouvre **son** navigateur,
sur la page où il en est. Touche l'image pour cliquer, tape le texte dessous, puis « J'ai
fini » : il reprend, connecté. Ce que tu tapes va dans la page, pas dans la conversation.

Il faut Telegram et la console accessible en https (`[modules.console] public_url`, posé par
l'installation). Pour qu'il oublie un site : « déconnecte-toi de Leboncoin ».

### Tes mots de passe, comme KeePass (D111), en option

Pour qu'Orion se reconnecte seul quand un site le déconnecte, le gardien peut aussi garder ton
identifiant d'un site : l'identifiant, le mot de passe et, si tu veux, la clé de double
authentification. Orion remplit la page de connexion, mais c'est le gardien qui fournit les
valeurs, seulement sur ce site : ni le modèle ni le journal ne voient ton mot de passe.

- **Depuis ton téléphone** : dis à Orion « garde mon identifiant Doctolib ». Il t'envoie le
  formulaire du gardien sur Telegram.
- **Sur le serveur** : `sudo /srv/orion/app/deploy/orion-admin logins add doctolib --site
  doctolib.fr --user moi@mail.fr --totp`. Le mot de passe et la clé 2FA sont demandés, jamais
  tapés dans la commande.
- **Depuis ton gestionnaire** : exporte-le en CSV (KeePass, KeePassXC, Bitwarden, Chrome,
  Firefox, 1Password), copie-le sur le serveur, puis
  `sudo /srv/orion/app/deploy/orion-admin logins import - < export.csv`, et efface le fichier :
  il contient tes mots de passe en clair.

`logins list` montre les identifiants gardés, `logins remove <nom>` en retire un. Un code reçu
par SMS ou par mail : Orion te le demande. Un captcha : il te propose de te connecter depuis
Telegram.

Pour qu'Orion envoie des mails ou des formulaires seul, ajoute `"mail_send"` ou
`"browser"` à `[guardian] autonomable` : il ne pourra le faire que dans les limites
que tu lui donnes en accordant l'autonomie (à quels destinataires, sur quels sites).

## Rappels de lieu (iPhone)

« Rappelle-moi d'acheter du pain quand je passe à la boulangerie. » Active `[modules.places]`
(`enabled = true`), relance `orion serve`, puis, sous l'utilisateur du cœur :

```bash
orion places token     # le jeton du téléphone (en refaire un rend l'ancien inutilisable)
```

Sur l'iPhone, app **Raccourcis** > **Automatisation** > **Arrivée** (ou **Départ**) > choisis le
lieu > action **Obtenir le contenu de l'URL** :
- URL : `https://<ton domaine>/places/event`, méthode **POST** ;
- en-tête `Authorization` : `Bearer <jeton>` ;
- corps JSON : `{"place": "boulangerie", "event": "arrive"}` (`"leave"` pour un départ).

Coche « Exécuter immédiatement ». Orion ne reçoit que le nom que tu as donné au lieu, et
seulement pour les lieux où tu as mis une automatisation : jamais de coordonnées.
