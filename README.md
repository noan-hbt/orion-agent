# Orion

Orion est un agent IA piloté par des événements. Il peut converser sur
plusieurs channels, utiliser des tools, suivre des tâches durables, attendre un
événement et se réveiller automatiquement.

```text
EVENT → TASK → RUN → ACTION / OBSERVATION → ANSWER ou WAIT → SLEEP
```

Le projet fonctionne sur un PC avec la CLI, ou sur un VPS en mode headless
avec Telegram, Discord, email ou webhook.

## Installation locale

Prérequis : Python 3.10 ou supérieur.

```bash
git clone <url-du-repo> Horizon
cd Horizon
python -m venv .venv
```

Linux/macOS :

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

Windows PowerShell :

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Configurez Orion avec l'installateur :

```bash
python orion_install.py
```

L'installateur demande le modèle OpenRouter, la clé API, les channels et les
options de mémoire. Il écrit la configuration dans `orion.toml` et les secrets
dans `.env`.

Pour régénérer la configuration :

```bash
python orion_install.py --force
```

Avec `--force`, la configuration précédente est sauvegardée dans un fichier
`orion.toml.backup*`.

Lancez ensuite Orion :

```bash
python orion_run.py
```

La CLI est active par défaut. Orion reste en fonctionnement continu jusqu'à
`Ctrl+C`.

### Interface CLI

La CLI conserve le prompt pendant les sorties asynchrones, rend le Markdown,
garde un historique local et accepte les messages multilignes.

- `Entrée` envoie le message ;
- `Alt+Entrée` ajoute une ligne ;
- `↑` et `↓` parcourent l'historique ;
- `/status` affiche l'état du runtime ;
- `/tools` liste les tools disponibles ;
- `/tasks` affiche les tâches récentes ;
- `/clear`, `/help` et `/exit` gèrent la session.

Les couleurs peuvent être désactivées avec `NO_COLOR=1` ou avec
`channels.cli.style = false`.

## Configuration

`orion.toml` centralise les paramètres de l'agent. Les chemins relatifs sont
résolus depuis le dossier de ce fichier.

Les principales sections sont :

| Section | Fonction |
|---|---|
| `[llm]` | modèle et paramètres OpenRouter |
| `[channels]` | channels activés et channel par défaut |
| `[runtime]` | tours maximum, priorités et préemption |
| `[response]` | réponses courtes et limite de longueur |
| `[reflection]` | pré-réflexion interne avant chaque run |
| `[context]` | limites et compaction du contexte |
| `[memory]` | extraction périodique des informations utiles |
| `[tools]` | emplacement et activation des tools installés |
| `[scheduler]` | réveils planifiés |
| `[tasks]` | stockage des tâches durables |

Pour autoriser plusieurs tools indépendants dans un même tour :

```toml
[runtime]
parallel_tool_calls = true
```

Les tools sans effet de bord peuvent alors s'exécuter en parallèle. Les tools
de tâche, du scheduler ou marqués comme ayant un effet de bord restent
séquentiels afin de préserver l'état.

Ne commitez jamais `.env`. Un modèle différent peut être choisi pour la
réflexion, la compaction et la mémoire :

```toml
[reflection]
enabled = true
model = "deepseek/deepseek-v4-flash-0731"
prompt_path = "REFLECTION_CORE.md"
max_input_chars = 12000
max_output_chars = 5000
temperature = 0.7
```

`REFLECTION_CORE.md` contient le prompt de la pré-réflexion. Cette réflexion
reste interne : elle n'est ni envoyée à l'utilisateur ni ajoutée à l'historique
de conversation.

## Channels

Les adaptateurs disponibles sont :

- `cli` ;
- `telegram` ;
- `discord` ;
- `email` ;
- `web`, `api` et `webhook`.

Activez un channel dans `orion.toml`. Exemple Telegram :

```toml
[channels]
enabled = ["telegram"]
default = "telegram"

[channels.telegram]
enabled = true
token_env = "TELEGRAM_BOT_TOKEN"
parse_mode = "HTML"
max_message_chars = 3500
```

Puis ajoutez le token dans `.env` :

```dotenv
OPENROUTER_API_KEY=...
TELEGRAM_BOT_TOKEN=...
```

Les réponses Markdown courantes sont converties en HTML pour Telegram et les
messages longs sont automatiquement découpés.

## Tools

Les tools sont des extensions indépendantes installées dans `tools/` :

```bash
python orion_tools.py install ./tool_packages/terminal
python orion_tools.py install ./tool_packages/web
python orion_tools.py list
```

Tools fournis :

- `terminal` : exécute des commandes locales, avec timeout et limites de sortie ;
- `web` : recherche et lecture de pages Web sans clé API.

Le terminal est limité au dossier du projet par défaut. Cette restriction peut
être modifiée dans `[tools.terminal]` ; activez-le uniquement si vous acceptez
les effets de bord possibles.

### Tools depuis GitHub

Le catalogue interactif permet de parcourir un dépôt contenant des paquets
Orion :

```bash
python orion_toolbox.py --repo noan-hbt/orion-tools
```

Un tool doit contenir un `tool.toml` et un module d'entrée exposant
`register(client)` ou `register(client, context)`. Voir
[tool_packages/echo](tool_packages/echo) pour un exemple.

Les tools installés sont chargés automatiquement au démarrage. Pour les
mettre à jour ou les supprimer :

```bash
python orion_tools.py update --all
python orion_tools.py remove <tool-id> --yes
```

## Tâches, rappels et attente

Orion crée une tâche lorsqu'une demande implique un objectif durable ou un
travail important. Une tâche peut contenir un état, un plan mutable, des runs,
des actions, des artefacts et un historique.

Pour un rappel ou une information attendue, Orion peut mettre la tâche en
`WAITING`. Il dort ensuite jusqu'à l'arrivée de l'événement correspondant : il
ne vérifie pas continuellement si quelqu'un a répondu.

Les actions à effet de bord sont enregistrées dans un ledger persistant afin de
réduire les répétitions accidentelles.

L'historique unifié est conservé dans `data/conversations.jsonl`. Les messages
de différents channels peuvent partager une même conversation grâce à leur
`conversation_id`.

## Déploiement VPS

Sur un VPS, utilisez l'installateur headless :

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export OPENROUTER_API_KEY="..."
export TELEGRAM_BOT_TOKEN="..."

python3 orion_vps_install.py \
  --channels telegram \
  --systemd \
  --force
```

L'installateur génère `orion.toml`, `.env` et `orion.service`. Il n'active pas
la CLI par défaut et ne demande aucune interaction.

Le compte Linux utilisé par systemd doit exister. Par défaut, il s'appelle
`orion` :

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin orion
```

La commande d'activation est affichée par l'installateur. Pour superviser le
service :

```bash
sudo systemctl status orion
journalctl -u orion -f
```

Voir [DEPLOYMENT_VPS.md](DEPLOYMENT_VPS.md) pour les détails du déploiement.

## Dépannage

Afficher les erreurs du processus :

```bash
python orion_run.py
```

Sur un VPS :

```bash
journalctl -u orion -f
```

Si Telegram ne répond pas, vérifiez `TELEGRAM_BOT_TOKEN`, le channel activé et
les éventuels `allowed_chat_ids`. Si une réponse est trop longue, Orion la
découpe automatiquement ; la limite peut être ajustée avec
`channels.telegram.max_message_chars`.

## Structure utile

```text
orion_config.py       configuration et bootstrap
runtime.py            cycle événementiel et boucle agentique
openrouter_client.py  client OpenRouter et tool calling
event_handler.py      file d'événements priorisée
tasks.py              tâches, plans, runs et actions
channels.py           contrat et routage multi-channel
channel_adapters.py   adaptateurs CLI, Telegram, Discord, email et webhook
reflection_engine.py  pré-réflexion interne
context_assembler.py  réduction et compaction du contexte
tool_manager.py       installation et chargement des tools
```
