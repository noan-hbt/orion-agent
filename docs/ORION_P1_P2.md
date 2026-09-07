# P1/P2 — primitives opérationnelles

Les composants suivants sont locaux, bornés et opt-in :

- `outbox.Outbox` : file SQLite durable avec idempotence, leases, retry,
  dead-letter (`failed`) et métriques.
- `budgets.BudgetTracker` : plafonds de durée, tours, tokens, coût et appels,
  avec `CircuitBreaker`, `RateLimiter` et backoff jitterisé pour OpenRouter.
- `observability` : logs JSON avec champs sensibles masqués, compteurs et commandes
  `python orion_run.py --command doctor|health|readiness`.
- `memory_store.MemoryStore` : mémoire locale cloisonnée par namespace, TTL,
  consentement et suppression explicite.
- `approvals.ApprovalStore` / `workflows.WorkflowEngine` : approbations
  persistantes et workflows séquentiels pouvant se mettre en attente.

Ces primitives n'activent pas automatiquement des effets externes : leur
intégration dans une politique d'autorisation complète et dans la planification
multi-agents est volontairement reportée.
