# Équipes locales Orion

Une équipe est un groupe de processus Orion qui partagent une base SQLite. Chaque
processus possède un `instance_id`; le nom `team` sépare les équipes qui
utilisent le même fichier. Le bus ne contacte aucun service externe.

```toml
[teams]
enabled = true
path = "data/teams.sqlite3"
instance_id = "planner"
team = "project"
poll_interval = 1.0
```

L'instance émettrice utilise `send_team_message` pour une information et
`delegate_team_job` pour une tâche. Une délégation reçoit un identifiant et
conserve son état (`queued`, `in_progress`, `completed` ou `failed`) ainsi que
son résultat. L'instance destinataire reçoit `team.message` ou `team.job` dans
son runtime, puis clôture une tâche avec `complete_team_job`. `get_team_job`
permet de vérifier le résultat sans réinjecter toute la conversation.

Les corps sont limités par `max_message_chars`; les messages sont adressés et
persistants après redémarrage. Le poller revendique atomiquement un message
avant de tenter son insertion dans la file locale, puis conserve ce claim
jusqu'à l'acquittement du runtime. Si la file est saturée ou si le processus
tombe, le claim expire et le message est repris. Cette livraison reste
`at-least-once` : une reprise peut survenir après une exécution commencée.
Le traitement applicatif reste coopératif et borné par les limites normales
du runtime.

## Cycle de vie d'un sous-agent

Un sous-agent est une définition persistante (identité, modèle, prompt,
capacités et tools autorisés). La soumission d'un objectif crée un job et une
session distincts. Le job évolue normalement de `queued` à `running`, puis
`completed` ou `failed`. Il peut aussi passer à `waiting` lorsqu'il appelle
`wait_for_input`; `send_to_subagent` ajoute alors le message à la session et
remet le job en file. Une session conserve l'historique LLM borné et permet de
reprendre le même objectif sans recréer le contexte.

L'état est écrit de manière durable dans `data/subagents.json` (ou le chemin
configuré). Au redémarrage, les jobs restés `running` sont reclassés en
`queued`, marqués comme repris après redémarrage, puis exécutés à nouveau. Les
jobs terminaux sont conservés dans la limite d'historique configurée; leurs
sessions sont supprimées avec eux lorsqu'elles ne sont plus référencées.

## Pause, reprise et annulation

`pause_subagent_job` est une demande coopérative: un job en cours termine son
appel courant (notamment un tool) avant de passer à `waiting`. Un job encore en
file peut être mis en attente immédiatement. `resume_subagent_job` remet en
file un job `waiting`; `send_to_subagent` fait de même en ajoutant une
information à la session. Une annulation est immédiate pour un job `queued` ou
`waiting`, mais seulement demandée pour un job `running`. Elle ne peut donc
pas interrompre rétroactivement un processus externe déjà lancé ni annuler un
effet qui a déjà eu lieu.

Arrêter le manager pendant l'exécution marque les jobs actifs comme échoués;
cela ne garantit pas l'arrêt instantané d'un appel externe. Les limites de
turns, de taille de contexte/résultat, de sortie des tools et de concurrence
restent les bornes effectives du runtime.

## Permissions et isolation

Chaque définition de sous-agent porte une liste `allowed_tools`. Le runtime
refuse l'appel d'un tool absent de cette liste; les capabilities et le prompt
ne constituent pas, à eux seuls, une autorisation supplémentaire. La liste
vide signifie qu'aucun tool n'est autorisé pour cet agent. Les secrets et les
permissions de l'instance principale ne sont pas implicitement transférés.

Les jobs, sessions et handoffs sont également filtrés par `scope` et, lorsque
présents, par tâche parente et identifiant de corrélation. Une opération de
lecture, de reprise, de pause, d'annulation ou d'envoi vers un job hors scope
est refusée. Dans une équipe, le destinataire SQLite doit correspondre à
`instance_id`; un expéditeur peut observer son job mais seul le destinataire
peut l'acquitter ou le clôturer.

## Livraison et idempotence

La livraison des événements de sous-agents et du bus d'équipe est
`at-least-once`, jamais exactement une fois. Les claims SQLite expirent après
un crash ou un échec d'enqueue afin de permettre une reprise. Un crash après
le démarrage du traitement peut donc provoquer une seconde exécution.

Les consommateurs doivent dédupliquer avec l'identifiant du job/message, le
`handoff_id` et/ou la `state_version`, et rendre leurs effets externes
idempotents lorsque c'est possible. Avant de réessayer une action, consulter
l'état durable et le ledger approprié: une notification reçue ne prouve pas
que l'effet métier n'a été produit qu'une seule fois.

## Durée maximale et redémarrage

Chaque job est soumis à `max_runtime_seconds` (900 secondes par défaut dans
le manager). Cette limite est contrôlée entre les tours du modèle et avant le
traitement des appels de tools. Elle est donc coopérative: elle ne coupe pas
un appel externe déjà en cours; celui-ci peut dépasser légèrement la limite
avant que le job soit marqué `failed` pour dépassement de durée.

Lors d'un redémarrage, un job `running` sans annulation demandée revient à
`queued` et est rejoué avec sa session persistante. Un job `running` dont
`cancel_requested` était déjà positionné est au contraire finalisé en
`cancelled` et n'est pas relancé. Les jobs `completed`, `failed` et `cancelled`
sont terminaux: il n'existe pas de reprise directe d'un job annulé; pour
retenter l'objectif, créer une nouvelle soumission après avoir vérifié les
effets éventuellement déjà produits.

L'arrêt normal du manager marque les jobs encore actifs comme échoués et
publie une notification durable. Les workers externes ne sont pas forcément
tués instantanément; toute action non idempotente doit donc être protégée par
le ledger ou une clé de déduplication.

## Redaction et rétention de l'outbox

Avant l'écriture de la session, les contenus de messages et de résultats sont
passés par la redaction des formes courantes de secrets (Bearer, API keys,
tokens, mots de passe et clés `sk-`). Cette protection est défensive et ne
remplace pas l'interdiction de transmettre des secrets dans un prompt ou un
résultat. Les métadonnées de routage peuvent également être persistées:
éviter d'y placer des credentials.

Les notifications de l'outbox sont conservées jusqu'à leur publication. Les
notifications déjà publiées sont limitées à la queue historique configurée
(`history_limit`); les notifications en attente ne sont jamais élaguées par
cette limite afin de permettre leur retransmission après crash. Une
republication est possible et doit être traitée comme un événement
`at-least-once`, en utilisant sa clé composée du handoff et de la version
d'état pour la déduplication.
