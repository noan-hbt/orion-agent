# Déployer Orion sur un VPS

Le VPS doit simplement exécuter le dépôt Orion avec Python et ses dépendances.
Les channels distants (Telegram, Discord, webhook ou email) tournent ensuite
en continu ; le channel CLI n'est pas activé par défaut dans ce mode.

## Installation headless

Depuis le dossier du dépôt :

```bash
python3 -m pip install .
python3 orion_vps_install.py \
  --channels telegram,discord \
  --set-secret OPENROUTER_API_KEY=... \
  --set-secret TELEGRAM_BOT_TOKEN=... \
  --set-secret DISCORD_WEBHOOK_URL=... \
  --systemd --force
```

L'installateur écrit `orion.toml`, `.env` et `orion.service`. Les secrets ne
sont jamais écrits dans `orion.toml`. Le fichier `.env` reçoit les permissions
`600` sur Linux et les écritures sont atomiques. En cas de `--force`, les
fichiers remplacés sont conservés sous `.backup` (puis `.backup.1`, etc.).
Le répertoire `data/` doit être accessible en écriture par le compte du service :

```bash
sudo install -d -o orion -g orion -m 700 data
sudo chown -R orion:orion data
```

Avant l'activation, le compte Linux indiqué par `--service-user` (par défaut
`orion`) doit exister et avoir accès au dossier du dépôt.

## Activation du service

La commande affichée par l'installateur copie l'unité dans systemd, recharge
la configuration et démarre Orion. L'unité est vérifiée avec
`systemd-analyze verify` lorsqu'il est disponible. Elle applique un profil
durci (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`,
`UMask=0077`) et n'autorise l'écriture que dans `data/` et `.env`. Les limites
de fichiers/processus et les protections de démarrage évitent les boucles de
redémarrage incontrôlées.

```bash
sudo systemctl status orion
journalctl -u orion -f
```

Pour modifier la configuration : arrêter le service, relancer l'installateur
avec `--force`, puis faire `sudo systemctl restart orion`.

## Retour arrière

Si une nouvelle configuration ne fonctionne pas, arrêter le service puis
restaurer explicitement les sauvegardes :

```bash
sudo systemctl stop orion
sudo cp -p orion.toml.backup orion.toml
sudo cp -p .env.backup .env
sudo systemctl daemon-reload
sudo systemctl start orion
```

Choisir `.backup.1`, `.backup.2`, etc. si nécessaire.

Pour un déploiement sans systemd, lancer directement :

```bash
python3 orion_run.py --config orion.toml
```
