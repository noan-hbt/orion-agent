# Orion

Orion est un agent IA piloté par des événements. Il peut converser sur
plusieurs channels, utiliser des tools, suivre des tâches durables, déléguer à
des sous-agents indépendants, attendre un événement et se réveiller automatiquement.

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
- `/agents` affiche les sous-agents disponibles ;
- `/jobs` affiche les travaux délégués et leur état ;
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
| `[subagents]` | workers parallèles, modèle économique et limites de délégation |
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
- `web` : recherche publique avec extraits, puis lecture de pages Web ; Tavily
  peut être activé avec une clé gratuite pour améliorer la qualité.

Le terminal est limité au dossier du projet par défaut. Cette restriction peut
être modifiée dans `[tools.terminal]` ; activez-le uniquement si vous acceptez
les effets de bord possibles.

Le tool web utilise Tavily en priorité lorsqu’une clé est configurée, puis Bing
RSS et DuckDuckGo comme repli. Il retourne des titres, URL et extraits ; Orion doit lire les sources importantes avec
`web_fetch` avant de présenter un fait comme établi. Les recherches et lectures
identiques sont mises en cache cinq minutes par défaut :

```toml
[tools.web]
# auto utilise Tavily si TAVILY_API_KEY existe, sinon la recherche publique.
provider = "auto"
api_provider = "tavily"
api_key_env = "TAVILY_API_KEY"
api_url = "https://api.tavily.com/search"
search_depth = "basic"
topic = "general"
timeout = 20
max_results = 8
max_chars = 16000
max_bytes = 2000000
max_search_bytes = 1500000
search_engines = ["bing_rss", "duckduckgo_html", "duckduckgo_lite"]
cache_ttl = 300
cache_size = 128
allow_private = false
```

Après une mise à jour locale du paquet, réinstalle-le :

```bash
python orion_tools.py install --force ./tool_packages/web
```

Le toolbox configure automatiquement les paramètres déclarés par le manifeste
du tool après une installation ou une mise à jour. Les valeurs ordinaires sont
placées dans `[tools.web]` ; les champs secrets restent dans `.env` :

```bash
python orion_toolbox.py
```

Pour le gestionnaire non interactif, ajoute `--configure` à `install` ou
`update`.

Un tool peut déclarer son assistant dans son `tool.toml` :

```toml
[configuration]
section = "mon_tool"

[[configuration.fields]]
key = "api_key"
label = "Clé API"
type = "secret"
env = "MON_TOOL_API_KEY"

[[configuration.fields]]
key = "timeout"
label = "Délai (secondes)"
type = "integer"
default = 30
```

Les types disponibles sont `string`, `secret`, `choice`, `integer`, `number`
et `boolean`. Un champ secret est toujours écrit dans `.env`, jamais dans
`orion.toml`.

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

## Sous-agents

Orion peut créer, modifier, désactiver et supprimer des workers spécialisés.
Une délégation retourne immédiatement un `job_id` : le sous-agent continue dans
un worker séparé, tandis qu’Orion reste disponible. Les progrès utiles et le
résultat reviennent ensuite comme événements `subagent.progress`,
`subagent.completed`, `subagent.failed` ou `subagent.cancelled`, avec le channel
et la conversation d’origine.

Les définitions, les jobs et les sessions sont persistés dans
`data/subagents.json`. Un job en cours lors d’un redémarrage est replacé en
file avec son historique de session. Un sous-agent peut appeler
`wait_for_input` lorsqu’il lui manque une information : sa session passe en
attente et son worker est libéré ; Orion peut ensuite utiliser
`send_to_subagent` pour le reprendre. Chaque sous-agent possède
son modèle, son prompt, ses capacités, sa limite de tours et une liste explicite
de tools autorisés. Le terminal n’est pas autorisé par défaut : Orion doit le
donner explicitement à un worker d’exploration de fichiers.

```toml
[subagents]
enabled = true
state_path = "data/subagents.json"
workers = 3
default_model = "deepseek/deepseek-v4-flash-0731"
default_tools = ["web_search", "web_fetch", "fetch_url", "fetch_json_api"]
default_max_turns = 8
max_session_messages = 100
emit_progress_events = true
```

Une tâche durable déléguée peut utiliser `wait_for_event` avec
`event_type = "subagent.completed"` et le `job_id` attendu. Cela permet à Orion
de dormir jusqu’au résultat au lieu de surveiller le worker.

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
subagents.py          workers IA parallèles, jobs persistants et événements
openrouter_client.py  client OpenRouter et tool calling
event_handler.py      file d'événements priorisée
tasks.py              tâches, plans, runs et actions
channels.py           contrat et routage multi-channel
channel_adapters.py   adaptateurs CLI, Telegram, Discord, email et webhook
reflection_engine.py  pré-réflexion interne
context_assembler.py  réduction et compaction du contexte
tool_manager.py       installation et chargement des tools
```
