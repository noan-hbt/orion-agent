# Déployer Orion en deux processus (D28)

Deux utilisateurs Unix, un groupe commun :

```bash
sudo groupadd orion
sudo useradd --system --gid orion --home /srv/orion orion-guardian
sudo useradd --system --gid orion --home /srv/orion orion-core
sudo install -d -o orion-core -g orion -m 0750 /srv/orion
# le code, orion.toml et .venv (uv sync) dans /srv/orion, lisibles par le groupe ;
# data/ appartient à orion-core
```

Dans `orion.toml`, `[guardian] enabled = true`. Les secrets vont dans deux fichiers distincts :

- `/etc/orion/guardian.env` (0600, `orion-guardian`) : `INKBOX_API_KEY`, `INKBOX_SIGNING_KEY`,
  les adresses des agendas, les mots de passe mail, `ORION_FEDERATION_KEY` ;
- `/etc/orion/core.env` (0600, `orion-core`) : `OPENROUTER_API_KEY` seulement.

`orion serve` refuse de démarrer s'il voit un secret du gardien. Les commandes
d'administration d'Inkbox se lancent sous l'utilisateur du gardien :

```bash
sudo -u orion-guardian sh -c 'set -a; . /etc/orion/guardian.env; .venv/bin/orion inkbox setup'
```

Puis `sudo systemctl enable --now orion-guardian orion-core`. Le gardien crée la socket
(`run/guardian.sock`, mode 0660, groupe `orion`) et son registre (`guardian/`, mode 0700).
