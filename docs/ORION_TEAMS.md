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
