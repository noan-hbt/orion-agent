# Déployer Orion sur un VPS

Le VPS doit simplement exécuter le dépôt Orion avec Python et ses dépendances.
Les channels distants (Telegram, Discord, webhook ou email) tournent ensuite
en continu ; le channel CLI n'est pas activé par défaut dans ce mode.

## Installation headless

Depuis le dossier du dépôt :

```bash
python3 -m pip install -r requirements.txt
python3 orion_vps_install.py \
  --channels telegram,discord \
  --set-secret OPENROUTER_API_KEY=... \
  --set-secret TELEGRAM_BOT_TOKEN=... \
  --set-secret DISCORD_WEBHOOK_URL=... \
  --systemd --force
```

L'installateur écrit `orion.toml`, `.env` et `orion.service`. Les secrets ne
sont jamais écrits dans `orion.toml`. Le fichier `.env` reçoit les permissions
`600` sur Linux.

Avant l'activation, le compte Linux indiqué par `--service-user` (par défaut
`orion`) doit exister et avoir accès au dossier du dépôt.

## Activation du service

La commande affichée par l'installateur copie l'unité dans systemd, recharge
la configuration et démarre Orion. Le service redémarre automatiquement après
un arrêt ou une erreur de processus.

```bash
sudo systemctl status orion
journalctl -u orion -f
```

Pour modifier la configuration : arrêter le service, relancer l'installateur
avec `--force`, puis faire `sudo systemctl restart orion`.

Pour un déploiement sans systemd, lancer directement :

```bash
python3 orion_run.py --config orion.toml
```
