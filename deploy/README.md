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

Le script :

- installe Caddy, uv et Python ;
- crée les utilisateurs et place le code dans `/srv/orion/app` ;
- crée `/etc/orion/orion.toml` et deux fichiers de secrets :
  - `/etc/orion/guardian.env` (0600, `orion-guardian`) ;
  - `/etc/orion/core.env` (0600, `orion-core`) ;
- installe les services systemd et le pare-feu.

Tu peux le relancer pour mettre à jour : il ne touche jamais à la configuration ni aux
secrets.

Ensuite :

1. Remplis `/etc/orion/orion.toml` (ton numéro, le handle Inkbox d'Orion, tes prénoms) et les
   deux fichiers de secrets.
2. Crée l'identité Inkbox et abonne le webhook :

   ```bash
   sudo /srv/orion/app/deploy/orion-admin inkbox setup \
       --webhook-url https://orion.mondomaine.fr/inkbox/webhook
   ```

   Mets la clé de signature qu'il affiche dans `INKBOX_SIGNING_KEY`
   (`/etc/orion/guardian.env`). Puis envoie depuis ton téléphone la commande de connexion
   qu'il affiche.
3. Démarre :

   ```bash
   sudo systemctl restart orion-guardian orion-core
   sudo /srv/orion/app/deploy/orion-admin --core doctor
   ```

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
- **Mise à jour** : `sudo /srv/orion/app/deploy/install.sh orion.mondomaine.fr`.
- **Restaurer** : arrête les deux services, copie `journal.sqlite3` d'une sauvegarde dans
  `/srv/orion/data/`, supprime `memory.sqlite3` (reconstruit au démarrage), redémarre.

## Agir sur tes comptes (sessions connectées)

Pour qu'Orion agisse sur un site où tu as un compte (prendre un rendez-vous sur Doctolib,
suivre une commande), le gardien garde ta session, comme il garde tes mots de passe :

1. Sur ton ordinateur (il faut un écran) : `orion login doctolib https://www.doctolib.fr/sessions/new`.
   Une fenêtre s'ouvre ; connecte-toi, puis appuie sur Entrée dans le terminal. La session est
   écrite dans `doctolib.session.json`.
2. Copie le fichier sur le serveur, puis : `sudo deploy/orion-admin sessions add doctolib
   doctolib.session.json`, et efface le fichier copié.
3. Orion ne l'utilise que pour une tâche que tu approuves (`computer_task_send` avec cette
   session et ce texte exact), ou seule si tu lui as laissé ce genre de tâche sur ces sites.
   Chaque prêt est noté dans le registre du gardien.

`orion-admin sessions list` montre les sessions gardées, `sessions remove <nom>` en retire une.
Une session expire comme dans ton navigateur : refais l'étape 1 quand le site te déconnecte.

Pour qu'Orion envoie des mails ou des formulaires seul, ajoute `"mail_send"` ou
`"computer_task_send"` à `[guardian] autonomable` : il ne pourra le faire que dans les limites
que tu lui donnes en accordant l'autonomie (à quels destinataires, sur quels sites).
