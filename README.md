# Orion

Orion est un agent IA personnel piloté par événements. Il peut discuter via
plusieurs channels, utiliser des tools, suivre des tâches durables et déléguer
des travaux à des sous-agents indépendants.

## Démarrage rapide

Prérequis : Python 3.10 ou supérieur.

```bash
git clone <url-du-repo> Horizon
cd Horizon
python -m venv .venv
```

Activez l'environnement virtuel, puis installez les dépendances :

```bash
# Linux/macOS
source .venv/bin/activate

# Windows PowerShell
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
python orion_install.py
python orion_run.py
```

L'installateur configure le modèle OpenRouter, la clé API, les channels et les
options principales. La configuration est enregistrée dans `orion.toml` et les
secrets dans `.env`.

Pour reconfigurer une installation existante :

```bash
python orion_install.py --force
```

Ne commitez jamais `.env`.

## CLI

Orion reste actif jusqu'à `/exit` ou `Ctrl+C`.

| Commande | Fonction |
|---|---|
| `/help` | Afficher l'aide |
| `/status` | Voir l'état du runtime |
| `/tools` | Lister les tools chargés |
| `/tasks` | Lister les tâches durables |
| `/agents` | Lister les sous-agents |
| `/jobs` | Lister les jobs délégués |
| `/clear` | Nettoyer l'écran |
| `/exit` | Arrêter Orion |

La CLI accepte les messages multilignes avec `Alt+Entrée`, conserve un historique
local et affiche les réponses Markdown. Une alerte apparaît lorsqu'une requête
dépasse le délai configuré dans `channels.cli.slow_request_seconds`.

## Configuration

Le fichier `orion.toml` contient les paramètres de l'instance :

- `[llm]` : modèle et connexion OpenRouter ;
- `[channels]` : channels actifs et channel par défaut ;
- `[runtime]` : boucle d'exécution, parallélisme et événements reçus pendant un RUN ;
- `[response]` : longueur et style des réponses ;
- `[tools]` : emplacement et activation des tools ;
- `[subagents]` : workers, modèle par défaut et limites des sous-agents ;
- `[scheduler]` : rappels et réveils planifiés ;
- `[tasks]` et `[ledger]` : persistance des tâches et des actions.

Exemple minimal :

```toml
[llm]
model = "openai/gpt-4o-mini"
timeout = 60.0

[channels]
enabled = ["cli"]
default = "cli"

[channels.cli]
slow_request_seconds = 30.0
```

Pour utiliser plusieurs channels, ajoutez-les à `channels.enabled` et
configurez leur section correspondante. Les secrets sont toujours placés dans
`.env`, par exemple :

```dotenv
OPENROUTER_API_KEY=...
TELEGRAM_BOT_TOKEN=...
```

## Tools

Les tools sont des extensions installées dans `tools/`. Les tools fournis avec
le projet sont notamment `terminal` et `web`.

```bash
python orion_tools.py install tool_packages/terminal
python orion_tools.py install tool_packages/web
python orion_tools.py list
python orion_tools.py update --all
```

Pour découvrir et installer des tools depuis le dépôt GitHub configuré :

```bash
python orion_toolbox.py --repo noan-hbt/orion-tools
```

Le toolbox demande les paramètres nécessaires à chaque tool. Les clés API sont
écrites dans `.env`, les autres paramètres dans la section du tool de
`orion.toml`.

Un tool doit fournir un `tool.toml` et un module Python exposant `register()`.
Voir `tool_packages/terminal` pour un exemple.

## Tâches et sous-agents

Orion crée une tâche durable lorsqu'un objectif demande plusieurs étapes ou
doit être repris plus tard. Une tâche peut avoir un plan mutable, des actions,
des runs, des observations et un historique.

Pour les travaux moyens ou longs, Orion peut créer un sous-agent et lui
déléguer un job. Orion reste alors disponible. Le sous-agent peut :

- exécuter son travail indépendamment ;
- envoyer un résultat ou une question à Orion ;
- passer en attente avec `wait_for_input` ;
- reprendre sa session avec `send_to_subagent` ;
- être mis en pause, repris ou annulé.

Les événements reçus pendant un RUN peuvent être signalés entre deux tools.
Orion peut les traiter immédiatement ou les laisser pour un RUN séparé.

Les données persistantes sont stockées dans `data/`, notamment :

- `conversations.jsonl` : historique unifié des messages ;
- `tasks.json` : tâches durables ;
- `subagents.json` : sous-agents, jobs et sessions ;
- `action_ledger.sqlite3` : actions déjà exécutées.

## Déploiement VPS

Pour une instance sans interface graphique, utilisez l'installateur headless :

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python3 orion_vps_install.py \
  --channels telegram \
  --set-secret OPENROUTER_API_KEY=... \
  --set-secret TELEGRAM_BOT_TOKEN=... \
  --systemd
```

L'installateur génère la configuration, `.env` et éventuellement le service
systemd. Consultez [DEPLOYMENT_VPS.md](DEPLOYMENT_VPS.md) pour la configuration
du serveur et le suivi des logs.

## Dépannage rapide

```bash
python orion_run.py
```

Sur un VPS :

```bash
sudo systemctl status orion
journalctl -u orion -f
```

Si Telegram ne répond pas, vérifiez le token, la présence de `telegram` dans
`channels.enabled` et les éventuels `allowed_chat_ids`.
