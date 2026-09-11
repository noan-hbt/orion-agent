# Terminal tool security model

`orion.terminal` exécute des commandes locales avec quelques garde-fous applicatifs :

- le `cwd` reste sous la racine Orion par défaut ;
- la sortie et la durée sont bornées ;
- l'environnement du processus Orion n'est plus hérité en bloc. Seules quelques variables système nécessaires au shell (`PATH` et équivalents Windows/temp) sont copiées par défaut ;
- des variables supplémentaires peuvent être héritées explicitement avec `env_allowlist` dans `[tools.terminal]`.

Exemple :

```toml
[tools.terminal]
max_timeout = 120
max_output_chars = 12000
allow_outside_root = false
env_allowlist = ["CI", "HTTP_PROXY"]
```

Une variable telle que `OPENROUTER_API_KEY`, `TAVILY_API_KEY` ou tout autre secret n'est donc pas visible par une commande enfant sauf si son nom est explicitement ajouté à `env_allowlist`.

Ces contrôles **ne constituent pas un sandbox OS**. Une commande autorisée conserve les permissions du compte qui exécute Orion et peut potentiellement accéder au filesystem ou au réseau avec ces permissions. Pour une isolation forte du filesystem, du réseau, des processus ou des privilèges, exécutez Orion/le tool dans un conteneur, une VM ou un autre mécanisme d'isolation OS adapté.
