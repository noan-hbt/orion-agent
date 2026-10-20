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

Une skill ne donne jamais d'ordre à Orion contre son propriétaire : elle explique comment faire
une chose qu'il demande.
