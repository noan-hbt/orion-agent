# Orion Cockpit CLI

Le lancement interactif (`python orion_run.py`) utilise le cockpit terminal
`CockpitCLIAdapter`. Il remplace l'adaptateur CLI configuré avant le démarrage
du runtime, afin qu'un seul lecteur stdin soit actif. `cli_ui.py` et `cli_v2.py`
restent disponibles pour compatibilité.

Le cockpit accepte le langage naturel et les commandes `/status`, `/dashboard`,
`/watch`, `/commands` et `/exit`. Les commandes d'observation consultent
`CockpitBackend.snapshot()`; les autres entrées sont routées vers Orion puis
affichées au fil des réponses.

Les modes `--once` et `--command` restent non interactifs et ne démarrent pas
le lecteur stdin; `--output jsonl` conserve son contrat JSONL.
Les commandes se saisissent à l'invite ; `/quit` est un alias de `/exit` et il
n'existe pas de raccourci clavier global supplémentaire.

Tests cockpit : `python -m pytest -q tests/test_cli_cockpit.py tests/test_cli_cockpit_backend.py tests/test_cockpit_actions.py tests/test_cockpit_events.py tests/test_cockpit_observability.py tests/test_cockpit_keyboard.py`.
