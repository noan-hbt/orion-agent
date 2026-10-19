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
  gardien v0, constitution (personnalité), contexte conçu pour le cache de prompt, canal terminal.
- **M2 — mémoire v0** : faits bi-temporels avec provenance (jamais écrasés, gardés en historique),
  registre des engagements (je dois / on me doit), recherche plein texte, outils mémoire,
  extraction en arrière-plan, oubli par source vérifié par rejeu du journal, profil lisible
  (`orion memory`), oubli (`orion forget <source>`).
- **M3 — iMessage et initiative v0** : canal iMessage via Inkbox (polling ou webhook signé,
  dédoublonnage, reprise après crash, rafales regroupées, indicateur de frappe, réponses en
  bulles), outbox idempotente, approbations par tapback ou par mot, rappels durables
  (`remind_me`), moteur d'initiative (3 points par jour sur les engagements, alerte avant une
  échéance, heures calmes, budget d'attention, « pourquoi maintenant »), démon `orion serve`.
- **Adaptation** : Orion apprend ton manuel. Il applique tout de suite ce que tu lui dis
  (`adapt`, `initiative_settings`). Une réflexion périodique tire des règles de tes messages et
  de tes tapbacks. Les points que tu ignores ne sont plus envoyés. Tout est visible
  (`orion memory`), corrigeable, et oublié avec la conversation d'où il vient.
- **M4 — fournisseurs étendus** : routeur avec modèles de secours, vérificateur sur un autre
  modèle pour tout message proactif, délégation des tâches longues au CLI `claude` officiel (en
  arrière-plan, résultat transmis à la fin). Le CLI comme modèle principal reste à faire (D22).
- **Boîte de réception unifiée** : tous les canaux passent par une seule file vers ton fil
  (D23).
- **M5 v0 — la voix, par Inkbox** :
  - au téléphone, la voix d'Inkbox est tenue à jour avec un brief tiré de la mémoire d'Orion ;
  - après chaque appel, la transcription revient dans ton fil et Orion fait ce qui a été convenu ;
  - Orion peut t'appeler, te réveiller par un appel, ou appeler un tiers avec ton accord ;
  - la ligne est réservée à ton numéro.

Voir l'ordre de construction dans
[`docs/architecture/04-architecture.md`](docs/architecture/04-architecture.md).

## Démarrer

```bash
uv sync
cp orion.example.toml orion.toml
export OPENROUTER_API_KEY=...
uv run orion doctor
uv run orion chat
```

### iMessage

Il faut une clé Inkbox dans `INKBOX_API_KEY`. Dans `orion.toml`, section `[channels.imessage]`,
indique `enabled = true`, l'identifiant d'Orion (`identity`) et ton numéro (`owner_number`).
Ensuite :

```bash
uv run orion inkbox setup   # crée l'identité et affiche le message à envoyer depuis ton iPhone
uv run orion serve          # le démon : iMessage et initiative
```

Orion ne peut t'écrire qu'une fois que tu lui as écrit. Le mode `poll` (par défaut) n'a besoin
d'aucune URL publique. Pour le mode `webhook`, lance `orion inkbox setup --webhook-url
https://…` et place la clé de signature affichée dans `INKBOX_SIGNING_KEY`.

## Documentation

- [`docs/research/`](docs/research/) — études : Claude Code, paysage des agents, ce qui fait un Jarvis
- [`docs/architecture/04-architecture.md`](docs/architecture/04-architecture.md) — architecture
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — journal des décisions
