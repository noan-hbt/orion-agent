# Orion

Agent personnel toujours actif et proactif — un second cerveau qui vit sur un serveur, te parle par
iMessage et au téléphone, et sait collaborer avec d'autres instances d'Orion, y compris celles
d'autres personnes.

## État

- **M0 — fondations** : journal append-only (SQLite WAL, horloge logique hybride), modèle
  d'événements avec provenance et confiance, projections reconstructibles, superviseur,
  configuration typée, logs structurés, CI.
- **M1 — premier cerveau** : client OpenRouter (DeepSeek v4.1 Flash par défaut), boucle d'agent
  (arrêts typés, budgets, détection de boucles, outils parallèles), contrat d'outil fail-closed,
  gardien v0, constitution (personnalité), contexte conçu pour le cache de prompt, canal terminal. Voir l'ordre de construction dans
[`docs/architecture/04-architecture.md`](docs/architecture/04-architecture.md).

## Démarrer

```bash
uv sync
cp orion.example.toml orion.toml
export OPENROUTER_API_KEY=...
uv run orion doctor
uv run orion chat
```

## Documentation

- [`docs/research/`](docs/research/) — études : Claude Code, paysage des agents, ce qui fait un Jarvis
- [`docs/architecture/04-architecture.md`](docs/architecture/04-architecture.md) — architecture
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — journal des décisions
