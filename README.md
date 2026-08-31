# OpenRouter Agent Client

Client Python autonome pour appeler OpenRouter et construire un agent avec
historique de conversation et outils locaux.

## Installation

```bash
pip install -r requirements.txt
```

Définir la clé API dans l’environnement :

```bash
export OPENROUTER_API_KEY="sk-or-..."
```

## Appel simple

```python
from openrouter_client import OpenRouterClient

client = OpenRouterClient(
    model="anthropic/claude-sonnet-4.5",
    site_name="Mon agent",
    site_url="https://example.com",
)

answer = client.run("Explique les files FIFO en trois phrases.")
print(answer)
client.close()
```

Le modèle par défaut est `~openai/gpt-latest`; il peut être remplacé par
n'importe quel identifiant disponible dans le catalogue OpenRouter.

## Agent avec outil

```python
def get_weather(city: str) -> dict:
    # Remplacer par un vrai appel vers votre service météo.
    return {"city": city, "temperature_c": 21, "condition": "ensoleillé"}


client = OpenRouterClient(system_prompt="Tu es un assistant utile et précis.")
client.register_tool(
    "get_weather",
    get_weather,
    description="Retourne la météo actuelle d'une ville.",
    parameters={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    },
)

print(client.run("Quelle est la météo à Paris ?", max_tool_rounds=5))
```

Le client ajoute automatiquement le message assistant contenant `tool_calls`,
exécute chaque outil, ajoute un message `role="tool"`, puis rappelle le modèle
jusqu'à obtenir une réponse finale. Les erreurs d'outils sont renvoyées au
modèle sous forme JSON par défaut ; utiliser `raise_tool_errors=True` pour les
faire remonter immédiatement.

## Asynchrone et streaming

```python
import asyncio


async def main():
    async with OpenRouterClient() as client:
        client.register_tool("get_weather", get_weather,
                             description="Retourne la météo.",
                             parameters={"type": "object", "properties": {}})
        answer = await client.run_async("Donne-moi la météo.")
        print(answer)


asyncio.run(main())
```

Pour une réponse texte progressive, utiliser `stream_run(prompt)` ou
`stream_text(messages)`. Le streaming automatique de tool calls demande une
agrégation des deltas avant exécution ; la boucle `run`/`run_async` est donc le
chemin recommandé pour un agent outillé.

## File d'événements prioritaire

Le module [event_handler.py](event_handler.py) est indépendant du client LLM.
Il reçoit les événements applicatifs et les distribue à des handlers locaux.

```python
from event_handler import EventHandler, EventPriority, EventType


def handle_email(event):
    print("Nouveau mail :", event.payload)


events = EventHandler(workers=2, default_max_attempts=3)
events.register(EventType.EMAIL, handle_email)
events.start()

events.publish(
    EventType.EMAIL,
    {"from": "client@example.com", "subject": "Question"},
    priority=EventPriority.HIGH,
    source="imap",
)
events.publish(
    EventType.CRON,
    {"job": "daily-report"},
    priority=EventPriority.LOW,
    source="scheduler",
)

events.wait_until_empty(timeout=10)
events.stop()
```

Les priorités disponibles sont `LOW`, `NORMAL`, `HIGH` et `CRITICAL`. À
priorité identique, les événements sont traités dans leur ordre d'arrivée.
Les handlers peuvent être synchrones ou asynchrones. Les événements qui
échouent après toutes leurs tentatives sont accessibles via
`events.dead_letters`.

## Runtime dormant/réveillé

Le module [runtime.py](runtime.py) orchestre le cycle de vie de l'agent sans
appeler de LLM. Son cycle de base est `SLEEP -> EVENT -> WAKE -> RUN -> SLEEP`.
La future méthode `RUN` suivra les sous-phases
`REFLECTION -> TOOL (+ SMALL_OUTPUT) -> ANSWER` ou `NEW_TURN`.

```python
from event_handler import EventHandler, EventType
from runtime import AgentRuntime, RuntimeState

events = EventHandler(workers=2)
runtime = AgentRuntime().attach(events)

events.start()
runtime.start()

events.publish(EventType.WEBHOOK, {"action": "new_ticket"})
runtime.wait_until_empty(timeout=10)

assert runtime.state == RuntimeState.SLEEP
print(runtime.wake_context)
print(runtime.run_context.phase)

runtime.stop()
events.stop()
```

Le runtime possède sa propre file de réveil afin de ne pas coupler la file des
événements entrants à la future boucle d'exécution. Pour l'instant, `RUN`
crée uniquement un `RunContext` en phase `REFLECTION`, puis repasse en `SLEEP`.
Un `StateStore` personnalisé peut être fourni pour charger et sauvegarder
l'état depuis une base de données.

Avec un client OpenRouter, le runtime exécute la boucle complète :
`REFLECTION -> DECISION -> ACTION/TOOL -> OBSERVATION -> NEW_TURN`, puis
`ANSWER`, `WAIT` ou `COMPLETE`. Les tools de gestion de tâche sont ajoutés
automatiquement aux tools envoyés au modèle.

Les appels de tools sont séquentiels par défaut (`runtime.parallel_tool_calls =
false`). Cela permet au modèle de recevoir chaque observation, de produire une
progression contextualisée et de décider du tool suivant. Le mode parallèle
reste activable si la progression intermédiaire n'est pas nécessaire.

Le style de réponse est concis par défaut :

```toml
[response]
concise = true
max_chars = 3000
max_sentences = 8
```

Orion privilégie quelques phrases ou puces naturelles, sans répéter la
demande. La limite de caractères est un filet de sécurité appliqué avant
l'envoi au channel ; elle n'a pas vocation à couper les réponses normales.

```python
from event_handler import EventHandler, EventType
from openrouter_client import OpenRouterClient
from runtime import AgentRuntime

events = EventHandler(workers=1)
llm = OpenRouterClient(system_prompt="Tu es un assistant fiable.")
runtime = AgentRuntime(llm_client=llm).attach(events)

events.start()
runtime.start()
events.publish(EventType.MESSAGE, {"text": "Bonjour"})
runtime.wait_until_empty(timeout=60)
runtime.stop()
events.stop()
llm.close()
```

Lorsqu'une réponse du modèle contient un court texte avant un tool, ce texte
est maintenant envoyé immédiatement au channel comme sortie intermédiaire.
Lorsque plusieurs tools sont demandés dans le même tour, Orion envoie aussi un
accusé de progression entre les appels. La sortie finale est envoyée séparément
à la fin du RUN.

## Tasks durables

Le module [tasks.py](tasks.py) introduit l'objectif durable de l'agent. Une
`Task` contient son `objective`, son `status`, sa `priority`, son
`current_state`, son `plan`, ses `runs`, ses `actions`, ses `artifacts` et son
`history`.

Le runtime ne crée ni ne retrouve automatiquement une tâche à partir d'un
événement. L'événement réveille seulement l'agent ; c'est la future phase
`RUN` qui décidera d'ignorer l'événement, de créer une tâche avec
`runtime.create_task(...)`, ou de reprendre une tâche avec
`runtime.bind_task(...)`. Ainsi, un simple message peut rester sans tâche.

```python
from event_handler import EventHandler, EventType
from runtime import AgentRuntime
from tasks import JsonTaskStore

events = EventHandler(workers=1)
runtime = AgentRuntime(task_store=JsonTaskStore("data/tasks.json")).attach(events)

events.start()
runtime.start()
events.publish(
    EventType.WEBHOOK,
    {
        "message": "Le serveur est lent",
        "ticket": 42,
    },
)
runtime.wait_until_empty(timeout=10)

# Dans la prochaine implémentation, le RUN décidera par exemple :
task = runtime.create_task(
    "Identifier pourquoi le serveur est lent et corriger le problème."
)
task.set_plan([
    "inspect",
    "diagnose",
    "fix",
    "verify",
    "report",
], reason="plan initial créé par l'agent")
runtime.save_current_task(task)
print(task.id, task.objective, task.status, len(task.runs))
```

Pour poursuivre une tâche existante, l'agent peut la charger et la lier au RUN
courant :

```python
task = runtime.bind_task(task.id)
```

Chaque liaison crée un nouveau `TaskRun`. La décision, les actions, les
observations et la mise à jour de l'objectif sont modélisées dans
`RuntimeState`, mais leur boucle d'exécution sera implémentée dans une
prochaine étape.

Le plan est une liste d'objets `PlanStep`, mais accepte aussi des chaînes pour
un plan initial rapide. L'agent peut le remplacer avec `set_plan`, ajouter une
étape avec `add_plan_step`, ou modifier une étape avec `update_plan_step` : le
plan n'est donc pas un script rigide.

## WAIT : ne pas travailler inutilement

Une tâche peut déclarer une condition d'attente. Le run se termine, la tâche
passe à `WAITING`, et le runtime retourne dormir. Il ne fait aucun polling.

```python
condition = runtime.wait_current_task(
    event_type=EventType.EMAIL,
    payload_equals={"from": "paul@example.com"},
    description="Attendre les chiffres de juillet envoyés par Paul",
)
runtime.save_current_task()
```

Quand un nouvel email correspondant arrive, le runtime retrouve la tâche grâce
à sa condition persistée, la repasse à `RUNNING` et crée un nouveau `TaskRun` :

```python
events.publish(
    EventType.EMAIL,
    {"from": "paul@example.com", "body": "Voici les chiffres de juillet"},
)
```

Les conditions peuvent filtrer `event_type`, `source`, des champs du payload
et des métadonnées. La décision d'attendre appartient à l'agent ; l'événement
ne fait que satisfaire, ou non, une attente déjà enregistrée.

## V0.8 : Scheduler + autonomie

Le module [scheduler.py](scheduler.py) permet à l'agent de créer ses propres
réveils ponctuels. `schedule_current_task(...)` associe un schedule à la tâche
courante, enregistre automatiquement l'attente correspondante, puis le
runtime retourne dormir. Le scheduler publie ensuite un événement `SCHEDULE`
à l'échéance ; aucune boucle de polling n'est exécutée par l'agent.

```python
from datetime import datetime, timedelta, timezone

from scheduler import JsonScheduleStore, Scheduler

scheduler = Scheduler(
    events,
    store=JsonScheduleStore("data/schedules.json"),
)
scheduler.start()

schedule = runtime.schedule_current_task(
    scheduler,
    datetime.now(timezone.utc) + timedelta(hours=3),
    payload={"reason": "vérifier l'état du serveur"},
    description="Reprendre la tâche demain matin",
)
```

À l'échéance, le scheduler publie un événement avec `schedule_id` et
`task_id`. Le runtime ne crée pas de tâche : il utilise uniquement ces données
pour trouver une condition `WAITING` déjà déclarée par l'agent, puis crée le
run suivant.

Lorsqu'un réveil est créé depuis un événement entrant, le runtime conserve
également automatiquement le channel et le destinataire (`chat_id` Telegram,
destinataire email, etc.). La réponse produite au RUN du réveil est ensuite
remise au `ChannelRouter`. Le modèle n'a donc pas besoin d'un tool
`send_telegram` : il utilise `schedule_wakeup`, puis produit le contenu du
rappel lorsqu'il est réveillé.

## Historique unifié multi-channel

Le fichier `data/conversations.jsonl` constitue un journal unique pour les
messages reçus et produits par Orion. Les messages provenant de la CLI,
Telegram, email, web, etc. sont fusionnés dans le même contexte par défaut et
chaque entrée conserve `source`, `channel` et `conversation_id`. Le runtime
réinjecte uniquement les derniers messages dans le prompt afin de préserver la
continuité sans charger tout l'historique.

Chaque événement contient `created_at` en UTC et `local_time` selon le fuseau
de la machine. Les entrées du journal portent le champ `at`. Lorsqu'un tool
met volontairement le RUN en attente (`wait_for_event` ou `schedule_wakeup`),
le runtime envoie automatiquement un accusé de réception si le modèle ne
produit pas de second message texte.

Les tools de contrôle sont terminaux pour un RUN : après un `schedule_wakeup`
ou un `wait_for_event`, les tool calls suivants du même tour ne sont pas
exécutés. Cela évite notamment qu'un modèle programme un rappel puis termine
immédiatement la tâche dans le même message.

Les réglages sont disponibles dans `[prompt]` :

```toml
history_enabled = true
history_limit = 20
history_max_chars = 12000
```

La section `[context]` configure la compaction indépendante des composants :

```toml
compaction_enabled = true
compactor_model = "openai/gpt-4o-mini"
total_max_chars = 60000
compactor_input_chars = 30000
task_max_chars = 12000
event_max_chars = 10000
```

Une projection déterministe est appliquée avant l'appel au petit modèle. Les
résultats de tools et les actions sont également bornés afin d'éviter qu'un
snapshot complet d'une tâche ne s'enregistre récursivement dans la tâche.

Pour une installation multi-utilisateur, un adaptateur peut fournir
`conversation_id` ou `user_id` dans les métadonnées de l'événement afin de
séparer les historiques. Sans cette métadonnée, la valeur `default` fusionne
les channels, ce qui correspond au mode assistant personnel.

## V0.10 : Interruptions et préemption

Une tâche active peut être interrompue par un événement de priorité supérieure.
Le runtime sauvegarde le `TaskRun` courant avec le statut `PAUSED`, traite
l'événement prioritaire, puis peut reprendre les runs interrompus dans l'ordre
inverse des interruptions.

```python
task_a = runtime.current_task

# Appelé par le runtime lorsqu'un événement urgent arrive :
runtime.pause_current_task(
    reason="Incident serveur urgent",
    interrupted_by=urgent_event,
)

# Après le traitement de la tâche urgente :
runtime.resume_preempted_task()
```

La file d'événements conserve déjà l'ordre de priorité. La préemption ne crée
pas automatiquement la tâche urgente : l'événement réveille l'agent, qui décide
de la tâche à exécuter. Les interruptions imbriquées sont conservées dans une
pile (`A → B → C`, puis reprise de `B`, puis de `A`).
\n+## V0.11 : protection contre les actions répétées

Le runtime utilise [action_ledger.py](action_ledger.py), un registre SQLite
persistant, pour dédupliquer les tools à effet de bord. Une action est réservée
avant son exécution, puis marquée `succeeded` ou `failed`. Une action exacte
déjà réussie ou en cours n'est pas rejouée ; une action très proche sur la
même cible est aussi bloquée pendant `dedupe_window` secondes.

Les tools externes doivent déclarer leurs effets de bord :

```python
llm.register_tool(
    "send_email",
    send_email,
    description="Envoie un email",
    parameters={
        "type": "object",
        "properties": {
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["to", "subject", "body"],
        "additionalProperties": False,
    },
    side_effect=True,
    dedupe_window=86400,
)
runtime = AgentRuntime(
    llm_client=llm,
    action_ledger_path="data/action_ledger.sqlite3",
)
```

Les outils internes de gestion des tâches sont protégés automatiquement.
Le modèle reçoit seulement le résultat compact du ledger (`duplicate`, la
raison et le résultat précédent) ; l'historique complet reste hors contexte.
## V0.12 : prompt systeme et memoire

Le prompt systeme est compose en couches, dans cet ordre : coeur immuable,
personnalite, methodologie, profil utilisateur, preferences, memoire durable,
puis instructions du runtime. Le coeur n'est jamais modifie par l'extracteur.

```python
from datetime import time

from prompt_context import (
    ConversationJournal,
    MemoryExtractor,
    MemoryMaintenance,
    PromptContextStore,
)

prompt_store = PromptContextStore(
    "data/prompt_context.json",
    core_path="ORION_CORE.md",
)
journal = ConversationJournal("data/conversations.jsonl")
extractor = MemoryExtractor(
    llm,
    prompt_store,
    model="openai/gpt-4o-mini",
)
maintenance = MemoryMaintenance(journal, extractor, run_at=time(23, 0))
runtime = AgentRuntime(
    llm_client=llm,
    prompt_store=prompt_store,
    conversation_journal=journal,
    memory_maintenance=maintenance,
)
```

Le runtime journalise les conversations de façon compacte. La maintenance
traite les conversations par petits lots, chaque soir ou via
`maintenance.run_once()`. Elle ne peut mettre à jour que le profil, les
preferences et la memoire ; les secrets sont filtres et le coeur, la
personnalite et la methodologie restent hors de sa portee.
## Configuration Orion

Le fichier [orion.toml](orion.toml) centralise la configuration du LLM, des
evenements, du runtime, des taches, du scheduler, du prompt, de la memoire,
des tools et du ledger. Les valeurs sensibles restent dans `.env`.

```python
from orion_config import load_orion

orion = load_orion("orion.toml")
orion.start()

# ... publier des evenements sur orion.events ...

orion.stop()
```

Le chargeur accepte aussi `OrionConfig.from_mapping(...)` pour une
configuration construite par du code. Les chemins relatifs sont resolus depuis
le dossier du fichier TOML. Les parametres inconnus sont ignores, ce qui
permet d'ajouter des options de compatibilite sans casser le demarrage.

## Tools installables

Les tools externes sont des paquets Python indépendants. Un paquet contient un
`tool.toml` et un module d'entrée, par exemple :

```toml
id = "mon-outil"
name = "Mon outil"
version = "1.0.0"
entrypoint = "tool:register"
api_version = 1
```

Le module expose `register(client)` ou `register(client, context)` et utilise
ensuite l'API existante `client.register_tool(...)`. Le paquet d'exemple est
dans [tool_packages/echo](tool_packages/echo).

Installer et gérer les extensions :

```bash
python orion_tools.py install ./mon-tool
python orion_tools.py install https://example.org/mon-tool.zip
python orion_tools.py list
python orion_tools.py update mon-tool
python orion_tools.py update --all
python orion_tools.py remove mon-tool --yes
```

Les tools installés sont chargés automatiquement au démarrage. Par défaut,
les tools du dossier `tools/` sont tous chargés ; `[tools].enabled` permet de
faire une liste blanche et `[tools].disabled` de désactiver certains paquets.
Le gestionnaire conserve la source d'installation dans
`data/installed_tools.json`, ce qui permet à `update` de récupérer la version
suivante depuis la même source. Une extension étant du code Python, installe
uniquement des paquets provenant d'une source de confiance.

### Catalogue GitHub interactif

`orion_toolbox.py` est un utilitaire séparé pour parcourir un dépôt de tools
Orion. Il détecte les dossiers contenant `tool.toml`, permet de filtrer la
liste et télécharge le tool sélectionné :

```powershell
python orion_toolbox.py --repo mon-compte/orion-tools
python orion_toolbox.py --repo mon-compte/orion-tools --search web
python orion_toolbox.py --repo mon-compte/orion-tools --install provider.web
```

Un dépôt public ne nécessite pas de clé GitHub. Pour un dépôt privé, définir
`GITHUB_TOKEN` dans l'environnement. Le dépôt peut aussi être configuré ainsi :

```toml
[tools.github]
repo = "mon-compte/orion-tools"
ref = "main"
timeout = 20
```

Puis lancer simplement `python orion_toolbox.py`.

Le channel CLI utilise une interface légère sans dépendance externe : bannière
de démarrage, prompt `›`, sorties Orion distinctes et messages de progression
pour les tools. `NO_COLOR=1` ou `channels.cli.style = false` désactive les
couleurs.

Deux paquets de démonstration sont fournis :

```powershell
python orion_tools.py install .\tool_packages\terminal
python orion_tools.py install .\tool_packages\web
```

`orion.terminal` expose `terminal` pour exécuter une commande locale. Par
défaut, le répertoire de travail reste dans le projet et la durée ainsi que la
sortie sont limitées :

```toml
[tools.terminal]
max_timeout = 120
max_output_chars = 12000
allow_outside_root = false
```

Comme ce tool peut modifier la machine, il est marqué comme outil à effet de
bord et ses commandes doivent rester explicites.

`orion.web` expose `web_search` et `web_fetch`. La recherche utilise le HTML
de DuckDuckGo et la lecture utilise HTTP/HTTPS, sans clé API. Les adresses
privées sont bloquées par défaut afin d'éviter les accès involontaires au
réseau local :

```toml
[tools.web]
timeout = 15
max_results = 8
max_chars = 20000
max_bytes = 2000000
allow_private = false
```

Le moteur de recherche HTML peut évoluer indépendamment d'Orion ; il s'agit
d'une intégration sans garantie de stabilité comparable à une API officielle.
## V0.29 : multi-channel

Le module [channels.py](channels.py) definit un contrat independant du Core.
Chaque integration traduit ses messages en `InboundMessage`; le router les
publie comme evenements et envoie les `AgentOutput` vers le channel cible.

```python
from channels import ChannelRouter, InboundMessage

router = ChannelRouter(events, default_channel="cli")
router.register(mon_adaptateur)
runtime = AgentRuntime(llm_client=llm, on_output=router.route).attach(events)

router.receive(InboundMessage(
    channel="cli",
    payload={"text": "Bonjour Orion"},
    reply_to="session-1",
))
```

Le Core ne contient aucune logique Telegram, Discord, email ou web. Les
adaptateurs sont ajoutes par l'application ou par des plugins et leurs
parametres sont declares dans la section `[channels]` de `orion.toml`.
Installation interactive :

```bash
python orion_install.py
```

L'assistant demande le modele, la cle OpenRouter, les channels et l'activation
de la memoire. Il genere `orion.toml` et met a jour `.env`; la cle n'est jamais
inscrite dans le fichier TOML. Utiliser `--force` pour regenerer la
configuration.
Les adaptateurs fournis dans [channel_adapters.py](channel_adapters.py) sont
`CLIAdapter`, `HttpWebhookAdapter`, `TelegramAdapter`, `EmailAdapter` et
`DiscordWebhookAdapter`. Les tokens et mots de passe sont configures avec
`token_env`, `password_env` ou `webhook_url_env`, puis lus depuis `.env`.

Telegram convertit automatiquement le Markdown courant des réponses (`**gras**`,
`*italique*`, `` `code` ``, liens et blocs de code) en HTML Telegram. Ce
comportement est configurable avec `channels.telegram.parse_mode` (`HTML` par
défaut, `MarkdownV2` ou `null`). Les réponses longues sont automatiquement
découpées ; `channels.telegram.max_message_chars` règle la taille de chaque
message (3500 par défaut pour rester sous la limite Telegram).

Le CLI et les adaptateurs declares dans `channels.enabled` sont demarres
automatiquement par `load_orion()`. Pour une integration non fournie, il suffit
d'implementer `start(on_message)`, `send(output)` et `stop()` puis de
l'enregistrer avec `orion.channels.register(...)`.
## Execution continue

Pour maintenir les adaptateurs, le scheduler, le runtime et la maintenance
memoire actifs dans un seul processus :

```bash
python orion_run.py --config orion.toml
```

Le processus s'arrete proprement avec `Ctrl+C` ou `SIGTERM`. Les adaptateurs
CLI, Telegram, email et HTTP possedent leur propre boucle de fonctionnement.
Un `DiscordWebhookAdapter` est un endpoint sortant sans polling ; la reception
Discord entrante doit etre reliee a `receive_payload()` ou a un adaptateur
Gateway dedie.
