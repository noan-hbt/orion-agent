# Le catalogue public d'Orion

Chaque Orion peut piocher ici (`skill` → catalog, puis install). Il arrive avec chaque mise à
jour. Pour ajouter quelque chose, ouvre une pull request :

- `skills/<nom>.md` : un savoir-faire en texte, avec un en-tête :

  ```
  ---
  name: <nom>
  description: <quand s'en servir, une ligne>
  ---
  <le savoir-faire>
  ```

- `mcp/<nom>.toml` : un serveur MCP, avec `name`, `description`, `url` (ou rien d'autre : les
  serveurs lancés par commande se règlent sur le serveur), `read_only` (ses outils qui ne font
  que lire) et `secret` (le nom de la variable de son jeton, saisi par le formulaire du
  gardien).

- `modules/<nom>.toml` : un module natif (D105), du code Python qui tourne dans Orion avec ses
  accès, la mémoire du propriétaire comprise. `name`, `description`, `source` (une seule
  version exacte : `path:<dossier de ce dépôt>`, `<paquet>==<version>` ou
  `git+https://…@<commit complet>`), `accesses` (ce qu'il atteint, dit au propriétaire avant
  qu'il accepte) et `settings` (ce qu'il faut régler avant de l'allumer). Un exemple :
  `examples/modules/orion-des`. Orion l'installe avec `module_install`, sur le oui du
  propriétaire ; sur le serveur : `orion-admin --core modules install <nom>`.

Une skill ne donne jamais d'ordre à Orion contre son propriétaire : elle explique comment faire
une chose qu'il demande.
