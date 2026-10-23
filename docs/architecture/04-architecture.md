# Étape 4 — Architecture d'Orion

> Ce document traduit les décisions D1–D14 et les études des étapes 1 à 3 en une architecture
> concrète : composants, frontières de confiance, modèle d'exécution, modèle de données, contexte,
> et ordre de construction. Il sert de référence pour l'implémentation ; toute divergence ultérieure
> donne lieu à une nouvelle décision dans `DECISIONS.md`.
>
> Les sections 1 à 16 sont le plan d'origine. La section 17 dit ce qui est construit au
> 20/10/2026 (jusqu'à D84), et où le code s'est écarté du plan.

---

## 1. Principes directeurs

Chaque principe répond à une hypothèse de Claude Code qu'on a choisi de casser (H1–H9, étape 1) ou à
une leçon des systèmes étudiés.

| # | Principe | D'où il vient |
|---|---|---|
| 1 | **Tout est événement.** Un message de toi n'est qu'un événement parmi d'autres ; le noyau tourne en continu. | H1 |
| 2 | **Le journal est la vérité, le reste est une projection.** Mémoire, fils de conversation, tâches, index : tout se reconstruit depuis le journal append-only. | Managed Agents, H2 |
| 3 | **Le contexte est compilé à chaque appel**, jamais compressé. Une conversation est une vue sur le journal. | H2 |
| 4 | **Provenance et confiance sur chaque donnée**, propagées à tout ce qui en dérive. | Étape 3 §7.1 |
| 5 | **Ce qui protège est séparé de ce qui agit.** Le gardien est un autre processus, qui détient les secrets et exécute les effets. | D14, Instinct, Muse, Dots |
| 6 | **Jamais le trio mortel** (données privées + contenu non fiable + canal de sortie) dans un même contexte d'exécution. | Willison, étape 3 §6.3 |
| 7 | **Le silence par défaut**, l'attention comme ressource gouvernée. | D12, H6 |
| 8 | **La confiance se gagne** : autonomie par type d'action et par relation, promue avec ton accord. | H5, D12 |
| 9 | **Fini = preuve.** Une tâche est terminée quand son contrat de vérification passe. | H8 |
| 10 | **Des interfaces, pas des dépendances.** Fournisseurs, canaux, stockages, modèles vocaux : tout derrière un `Protocol`. | Leçon de l'ancien Orion |
| 11 | **Fail-closed partout** : un outil inconnu est dangereux, une politique absente est un refus, une erreur ne s'avale jamais. | Claude Code, ancien Orion |
| 12 | **Le cache de prompt est sacré** : préfixe stable, ajouts dynamiques en fin. | Claude Code, Hermes |

---

## 2. Vue d'ensemble

```
                                   ┌────────────────────────── CANAUX ───────────────────────────┐
                                   │ iMessage (Inkbox) · Telegram · Terminal · Voix (Pipecat)     │
                                   └──────────────┬───────────────────────────▲──────────────────┘
                                                  │ entrants                  │ sortants (via gardien)
┌───────────────────────────────── GARDIEN (processus séparé) ───────────────┴──────────────────┐
│ Pare-feu entrant (étiquetage de confiance, détection d'injection)                              │
│ Moteur de politiques (niveaux d'identité, autonomie, relations, planchers)                     │
│ Moniteur d'actions (classifieur aveugle au raisonnement, refuser-et-continuer)                 │
│ Coffre (identifiants, clés)  ·  Outbox (effets externes « effectivement une fois »)            │
└───────────┬───────────────────────────────────────────────────────────────▲──────────────────┘
            │ événements étiquetés                                          │ propositions d'action
┌───────────▼──────────────────────────────── NOYAU ────────────────────────┴──────────────────┐
│                                                                                               │
│  JOURNAL (append-only, SQLite WAL)  ──projections──▶  fils · tâches · mémoire · index          │
│        ▲                                                                                      │
│        │                         ┌──────────────── PROCESSUS COGNITIFS ──────────────────┐    │
│        │                         │ Réflexe   : tri des événements, petits modèles, < 1 s  │    │
│  Ordonnanceur ◀── minuteurs ──▶  │ Délibéré  : boucle d'agent, outils, sous-agents         │    │
│        │                         │ Réflexif  : consolidation, bilans, préparation (nuit)   │    │
│        │                         └──────────────▲───────────────────────────────────────┘    │
│        │                                        │ contexte compilé                            │
│  Moteur d'initiative ◀── candidats ──  Compilateur de contexte ◀── Mémoire (faits, personnes, │
│  (attention, budget, échelle)                                        engagements, coffre*)    │
│                                                                                               │
│  Fournisseurs : API (OpenRouter, Anthropic, OpenAI) · CLI-modèle (claude, codex) · CLI-agent  │
└───────────┬──────────────────────────────────────────────┬───────────────────────────────────┘
            │                                              │
┌───────────▼───────────┐                      ┌───────────▼────────────────────────────────────┐
│ EXÉCUTEURS (mains)    │                      │ FÉDÉRATION (A2A)                                │
│ sandboxes jetables    │                      │ identité, relations, capacités, négociateur    │
│ (bubblewrap/conteneur)│                      │ isolé, mandats, reçus                           │
└───────────────────────┘                      └────────────────────────────────────────────────┘
* le coffre est détenu par le gardien ; le noyau n'en voit que des références.
```

---

## 3. Frontières de confiance et processus

Orion tourne comme **quelques processus Unix** sur un serveur, qui communiquent par sockets locales
avec des messages typés (Pydantic, sérialisés en JSON). Pas de microservices : un seul utilisateur,
une seule machine pour commencer.

| Processus | Rôle | Détient | Ne détient jamais |
|---|---|---|---|
| `orion-guardian` | Pare-feu entrant, politiques, moniteur d'actions, coffre, outbox, connecteurs de canaux | Identifiants, clés privées, jetons des canaux | Le raisonnement de l'agent |
| `orion-core` | Journal, projections, mémoire, cognition, initiative, fournisseurs LLM | Clés des fournisseurs LLM uniquement | Identifiants des services externes |
| `orion-voice` | Passerelle Pipecat, modèle vocal temps réel | Clé du fournisseur vocal | Accès direct à la mémoire (passe par `orion-core`) |
| `orion-exec` (éphémères) | Exécution de code et commandes | Rien de persistant | Réseau libre, secrets |
| `orion-negotiator` (éphémères) | Échanges avec l'Orion d'autres personnes | Une enveloppe de divulgation | Mémoire complète, coffre |

**Pourquoi le gardien détient les canaux** : envoyer un message est un effet externe. Le noyau ne
peut que *proposer* un envoi ; le gardien applique la politique, puis exécute via l'outbox. Un noyau
compromis par une injection ne peut donc pas exfiltrer sans passer par le contrôle.

**Le trio mortel, appliqué par construction** :
- tout contenu entrant reçoit une **étiquette de confiance** (§5) ;
- un contexte qui contient du contenu non fiable ne peut appeler **aucun outil à effet externe** : il
  produit une *proposition* que le gardien réévalue sans le contenu non fiable (aveugle au
  raisonnement) ;
- le négociateur et les exécuteurs n'ont ni la mémoire complète ni un canal de sortie libre.

---

## 4. Le noyau

### 4.1 Journal et projections

- **Journal** : table SQLite append-only (mode WAL). Chaque entrée : `id` (ULID), `hlc` (horloge
  logique hybride), `kind`, `actor`, `payload`, `provenance`, `trust`, `causation_id`,
  `correlation_id`. Rien n'est jamais modifié ; une correction est un nouvel événement.
- **Projections** : des lecteurs qui consomment le journal et maintiennent des tables dérivées
  (fils de conversation, tâches, faits, personnes, engagements, index). Elles sont **reconstructibles**
  : supprimer une projection et rejouer le journal doit redonner le même état. C'est aussi ce qui
  rend l'oubli vérifiable (§6.4).
- **Écriture** : un seul écrivain (le noyau) ; lecture concurrente libre. Suffisant pour un
  utilisateur ; Postgres plus tard si besoin, derrière la même interface `EventStore`.

### 4.2 Ordonnanceur et concurrence

- `asyncio` de bout en bout, **concurrence structurée** (`TaskGroup`) : annuler un parent annule ses
  enfants ; une tâche détachée l'est explicitement.
- Minuteurs durables (dans le journal) : réveils, échéances, résumés. Ils survivent aux redémarrages
  et ne dépendent **pas** d'un tour de conversation.
- Superviseur à la Erlang : un composant qui plante est redémarré et l'incident journalisé ; après N
  redémarrages, escalade. **Jamais de mort silencieuse** (leçon de l'ancien Orion).

### 4.3 Trois processus cognitifs

| Processus | Déclenché par | Modèles | Budget | Exemple |
|---|---|---|---|---|
| **Réflexe** | Chaque événement | Règles + petit modèle (Haiku-classe ou local) | Millisecondes à 1 s | « Ce mail mérite-t-il quoi que ce soit ? » |
| **Délibéré** | Un message de toi, une tâche, un candidat retenu par l'initiative | Modèle fort (API ou CLI-modèle) | Secondes à minutes | Répondre, planifier, agir |
| **Réflexif** | Heures calmes, inactivité, minuteurs | Modèle moyen, en lot | Minutes, de nuit | Consolider la mémoire, extraire les engagements, préparer le brief du matin |

### 4.4 Boucle d'agent (processus délibéré)

Reprise du meilleur de Claude Code (étape 1 §2), avec nos ajouts :
- générateur `async` qui rend des événements (texte, appels d'outils, progrès) ;
- **raison de fin typée** : `completed`, `verified`, `needs_input`, `budget_exhausted`,
  `max_turns`, `refused`, `error` ;
- outils en lecture lancés en parallèle, outils à effet sérialisés, ordre déterministe ;
- **budgets hiérarchiques** (tokens, argent, temps, appels) appliqués par le code ;
- disjoncteurs sur toute boucle de reprise ;
- **détection de répétition** et registre de progrès (Magentic-One) contre les boucles ;
- **fin = contrat** : une tâche qui déclare un critère de vérification n'est `verified` qu'après
  passage d'un vérificateur distinct, idéalement **un autre modèle**.

### 4.5 Contrat d'outil

```python
class Tool(Protocol):
    name: str
    description: str
    input_model: type[BaseModel]
    effect: Effect                 # NONE | INTERNAL | EXTERNAL  (défaut : EXTERNAL)
    reversibility: Reversibility   # REVERSIBLE | UNDO_WINDOW | IRREVERSIBLE (défaut : IRREVERSIBLE)
    accepts_untrusted_context: bool  # défaut : False
    concurrency_safe: bool           # défaut : False
    max_output_chars: int
    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult: ...
```

- Valeurs par défaut **fermées**.
- Un outil `EXTERNAL` ne s'exécute jamais dans le noyau : il devient une `ActionProposal` envoyée au
  gardien.
- Les résultats trop longs vont dans un fichier, l'agent reçoit un aperçu et un chemin.
- **MCP** : un adaptateur transforme chaque outil MCP en `Tool` (effet `EXTERNAL` par défaut,
  chargement différé par recherche).

### 4.6 Fournisseurs de modèles

```python
class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...
    capabilities: ProviderCapabilities  # outils, cache, vision, latence typique, coût
```

Trois familles (D5, D5-bis) :
- **API** : OpenRouter, Anthropic, OpenAI — contrôle total du contexte et du cache.
- **CLI-modèle** : binaire `claude` officiel en `stream-json` un seul tour, outils d'Orion annoncés
  par MCP et exécutés par Orion ; idem `codex app-server`. Utilise les abonnements. Latence plus
  forte : réservé au délibéré, jamais au réflexe ni à la voix.
- **CLI-agent** : on confie une tâche entière au CLI (ex. du code), il rend un résultat et des
  artefacts.

Un **routeur** choisit par requête selon le processus cognitif, le coût, la latence, la capacité, et
la règle « vérificateur ≠ générateur ». Repli automatique si un fournisseur échoue.

---

## 5. Confiance, provenance, contamination

Chaque donnée porte une `Provenance` :

```python
class Provenance(BaseModel):
    source: SourceRef            # ex. message:imessage:<id>, mail:<id>, web:<url>, peer:<instance>
    actor: ActorRef              # qui l'a produite (toi, Orion, une instance, un tiers)
    derived_from: list[EventId]  # chaîne de dérivation
    trust: Trust                 # OWNER > OWNER_INSTANCE > TRUSTED_PEER > THIRD_PARTY > WEB > UNKNOWN
    observed_at: HLC
```

Règles :
- **La confiance d'une donnée dérivée est le minimum de celles de ses sources.** Une consolidation ne
  peut jamais augmenter la confiance.
- Le contexte compilé porte la **confiance minimale** de ce qu'il contient ; elle conditionne les
  outils disponibles (§3).
- Rien de non fiable ne devient **mémoire procédurale** ni **instruction** sans ta confirmation.
- Quand une source est déclarée malveillante, le noyau suit `derived_from` en avant et **met en
  quarantaine** tout ce qui en dépend : souvenirs, tâches, messages en attente (traçage de contacts).

---

## 6. Mémoire

### 6.1 Stockage

| Couche | Stockage | Rôle |
|---|---|---|
| Épisodes | Le journal lui-même | Vérité immuable |
| Faits | Table `facts` bi-temporelle (`valid_from`, `valid_to`, `known_from`, `known_to`) + provenance + confiance | Ce qu'Orion croit, et depuis quand |
| Personnes et entités | Tables `entities`, `aliases`, `relations` (temporelles) | Résolution des personnes, fusions réversibles |
| Engagements | Table `commitments` : débiteur, créancier, objet, échéance, déclencheur (temps/événement/personne/lieu), état, preuve de clôture | Mémoire prospective |
| Profil lisible | Fichiers Markdown (`profile.md`, `people/*.md`, `projects/*.md`) versionnés | Ce que tu peux lire et corriger toi-même |
| Coffre | Détenu par le gardien | Identifiants, données sensibles, jamais rappelés par défaut |
| Index | SQLite FTS5 + `sqlite-vec` + tables de graphe | Recherche hybride, reconstructible |

### 6.2 Écriture

- **Chemin direct** : « souviens-toi que… », corrections explicites → effet immédiat, confiance
  `OWNER`.
- **Arrière-plan** (processus réflexif) : extraction des faits et engagements, résolution des
  personnes, détection des contradictions, invalidation datée (jamais d'écrasement), mise à jour du
  profil. Opérations `ADD / UPDATE / INVALIDATE / NOOP`, journalisées et réversibles.
- **Contradictions** : classées (changement réel, correction, désaccord de sources, erreur
  d'extraction, état temporaire) ; seules les deux premières invalident.

### 6.3 Lecture

- **Noyau toujours présent** (~2K tokens, reconstruit chaque nuit) : qui tu es, projets actifs,
  engagements ouverts, préférences clés.
- **Récupération hybride** : mots-clés + vecteurs + graphe (personnes, projets) + filtre temporel
  déduit de la question, fusion, diversité. **Récupération agentique** pour les questions difficiles.
- **Abstention** : la récupération renvoie un signal de couverture ; Orion dit « je ne sais pas »
  plutôt que d'inventer.

### 6.4 Oubli

`forget(source=...)` : supprimer les épisodes de la source, recalculer les faits dont c'était le seul
appui, purger index et embeddings, poser une pierre tombale. **Vérification** : rejouer le journal
sans la source dans une projection fantôme et comparer ; tout écart est une fuite à corriger.

---

## 7. Le compilateur de contexte

Pas d'historique qui grossit puis qu'on compresse : à chaque appel, le compilateur **assemble** un
contexte depuis l'état, en couches ordonnées pour le cache.

```
┌ 1. Constitution (identité d'Orion, règles, style)          ← statique, cache global
├ 2. Catalogue d'outils (noms + description, schémas différés) ← stable par processus cognitif
├ 3. Noyau mémoire (~2K)                                      ← stable sur la journée
├ ─ frontière de cache ─
├ 4. Situation : tâche en cours, objectif, contrat, budget restant
├ 5. Éléments récupérés (faits, personnes, engagements, artefacts) avec provenance
├ 6. Fenêtre récente du fil (N derniers échanges, le reste accessible par recherche)
└ 7. Rappels dynamiques (heure, changements, résultats d'outils récents) ajoutés en fin
```

- Chaque couche a un **budget de tokens** ; le compilateur arbitre et journalise ce qu'il a omis.
- Un fil de conversation long n'est jamais « compressé » : les anciens échanges restent dans le
  journal et sont **récupérables** comme n'importe quel souvenir.
- Les instructions données à l'oral (« ne pousse pas ») deviennent des **contraintes** stockées,
  réinjectées en couche 4 tant qu'elles sont actives — elles ne peuvent plus se perdre.

---

## 8. Moteur d'initiative (proactivité)

Implémente D12 (détail dans `03-jarvis.md` §4).

- **Sources** → événements normalisés (mail, agenda, minuteurs, veilles web, engagements, messages).
- **Cascade** : règles déterministes → tri par petit modèle (`pertinence, urgence, action possible,
  confiance, courbe de coût du retard`) → enrichissement en lecture seule → **arbitre d'utilité**.
- **Arbitre** : compare le coût du retard (fonction du temps) au coût d'interruption (heure, agenda,
  heures calmes, dernière réponse), sous un **budget quotidien d'attention**.
- **Décision** : ignorer · préparer en silence · prochain résumé · message au prochain moment de
  rupture · message immédiat · appel · proposer une action.
- **Livraison** : chaque message porte son « pourquoi maintenant » et une façon de répondre en un
  geste (réaction iMessage, un mot).
- **Apprentissage** : retours implicites et explicites ; les retours explicites deviennent des
  règles lisibles ; un journal de ce qui a été tu sert au bilan hebdomadaire « ce que j'ai failli te
  dire ».
- **Le travail en arrière-plan n'a que des outils en lecture.** Les écritures deviennent des
  propositions.

---

## 9. Gardien

- **Pare-feu entrant** : étiquette de confiance sur tout ce qui entre, détection d'injection
  (motifs + petit modèle), réécriture en données inertes pour les contenus à risque.
- **Moteur de politiques** (déterministe, données lisibles) :
  - niveaux d'identité (voix : L0–L3) ;
  - autonomie par type d'action (`approver` → `auto` sur promotion) ;
  - politiques par relation (fédération) ;
  - **planchers non contournables** : argent, mots de passe, suppression définitive, installation,
    envoi à un nouveau destinataire.
- **Moniteur d'actions** : un classifieur qui ne voit que tes messages et la proposition d'action,
  ni le raisonnement de l'agent ni les contenus non fiables. Refus renvoyé à l'agent comme résultat ;
  après 3 refus consécutifs ou 20 au total, escalade vers toi.
- **Approbations** : envoyées par iMessage (réaction ou mot-clé), avec l'action exacte, ses
  paramètres et son effet simulé (« voici le message qui partira »). Une approbation vaut pour
  **cette** action seulement.
- **Outbox** : chaque effet externe a une clé d'idempotence ; si le résultat est inconnu, on
  demande au lieu de réessayer.
- **Fenêtres d'annulation** pour les envois (quelques secondes à minutes selon le niveau de
  confiance).

---

## 10. Canaux

Tout passe par la boîte de réception unifiée (`channels/inbox.py`, D88). Un canal est un module
qui dépose les messages du propriétaire dans la boîte et lui fournit une sortie :

```python
class Outlet(Protocol):              # ce que fait tout canal
    name: str
    async def available(self) -> bool: ...
    async def deliver(self, text, correlation_id) -> None: ...   # par l'outbox du canal
    def working(self, item): ...     # accusé de lecture, « en train d'écrire »
class FileOutlet(Outlet): async def deliver_file(self, path, caption, correlation_id): ...
class AskingOutlet(Outlet): async def approve(self, proposal) -> Approval: ...
```

Réponses, messages d'initiative, fichiers, vocaux et demandes d'accord partent par le canal d'où
il a écrit en dernier, sinon le premier disponible. Ses réactions deviennent des
`feedback.reaction`, quel que soit le canal.

- **iMessage** (D9) : derrière une « ligne » (`channels/line.py`) ; Inkbox aujourd'hui, Blooio ou
  BlueBubbles demain, une classe chacun. Indicateur de frappe, réactions, bulles.
- **Telegram** (D88) : un bot à soi, gratuit et sans plafond ; boutons Oui / Non pour les accords.
- **Terminal** : développement et administration.
- **Voix** (D10) : `orion-voice` (Pipecat + Twilio) parle au noyau par des outils à deux vitesses
  (`consult_fast` < 1,5 s ; `start_task` → résultat dans l'appel ou par iMessage). Brief préparé
  pendant la sonnerie.
- **Un seul fil** quel que soit le canal : l'identité de l'interlocuteur et le fil sont résolus par
  le noyau, pas par le canal.

---

## 11. Fédération (collaboration entre instances)

Implémente D7, D8, D13. Construite en dernier, mais ses **primitives existent dès le départ** :
identité d'instance, provenance `peer:`, confiance `TRUSTED_PEER`, propositions typées.

- **Identité** : clé Ed25519 par instance, carte d'agent A2A signée, `did:web`.
- **Relations** : objet de premier rang (niveau, compétences autorisées, politique de divulgation,
  budgets, expiration, journal) ; la carte étendue vue par l'autre est générée depuis la relation.
- **Capacités** entrantes (Biscuit, ne font que restreindre) ; **mandats** sortants (intention
  pré-signée → engagement).
- **Négociateur isolé** : processus éphémère avec une enveloppe de divulgation et des secrets en
  prédicats (`acceptable(offre) -> bool`, quota d'appels).
- **Propositions typées** par domaine ; accords = engagements co-signés ; reçus mutuels.
- **Présentation** : par échange humain (code ou QR) ou par un ami commun ; contact à froid en
  quarantaine.
- **Instances d'un même utilisateur** : même protocole, niveau `OWNER_INSTANCE`, une seule instance
  de façade possède ton attention.

---

## 12. Modèle de données (aperçu)

| Type | Champs clés |
|---|---|
| `Event` | id, hlc, kind, actor, payload, provenance, trust, causation_id, correlation_id |
| `Thread` | id, participants, canaux, contraintes actives |
| `Task` | id, objectif, contrat de vérification, propriétaire, budget, état, parent, artefacts |
| `Fact` | sujet, prédicat, objet, valid_from/to, known_from/to, provenance, confiance |
| `Entity` / `Alias` | personne/lieu/projet, identifiants forts et faibles, historique de fusion |
| `Commitment` | débiteur, créancier, objet, échéance, déclencheur, état, preuve |
| `Candidate` / `Intervention` | pertinence, urgence, coût du retard, niveau, canal, pourquoi, issue |
| `ActionProposal` | outil, paramètres, effet simulé, réversibilité, contexte de confiance |
| `Policy` / `AutonomyGrant` | règle, source (toi / appris), portée, preuves |
| `Relationship` | pair, clés, niveau, compétences, divulgation, budgets, expiration |
| `Capability` / `Mandate` / `Receipt` | jetons, engagements, reçus co-signés |

---

## 13. Pile technique

| Besoin | Choix | Pourquoi |
|---|---|---|
| Langage | Python 3.12+ | D4 |
| Gestion du projet | `uv` | Rapide, reproductible |
| Typage et validation | Pydantic v2, `pyright` strict | Contrats explicites |
| Asynchrone | `asyncio` + `TaskGroup` | Concurrence structurée |
| Stockage | SQLite (WAL) + FTS5 + `sqlite-vec` | Un fichier, sauvegardable, suffisant pour un utilisateur |
| HTTP | `httpx` (client), Starlette/FastAPI (webhooks) | Standard |
| Logs et traces | `structlog` + OpenTelemetry (conventions GenAI) | Aucune erreur silencieuse |
| Tests | `pytest`, `pytest-asyncio`, tests d'évaluation séparés | — |
| Lint | `ruff` | — |
| Sandbox | `bubblewrap` (Linux) ou conteneur | Confinement OS |
| Voix | Pipecat | D10 |
| Fédération | SDK Python A2A, Biscuit (`biscuit-python`) | D13 |

---

## 14. Arborescence

```
orion/
  core/           journal, HLC, projections, ordonnanceur, superviseur, budgets
  cognition/      réflexe, délibéré, réflexif, boucle d'agent, vérificateurs
  context/        compilateur de contexte, couches, budgets de tokens
  memory/         faits bi-temporels, entités, engagements, index, consolidation, oubli
  initiative/     sources, cascade, arbitre, attention, livraison, apprentissage
  tools/          contrat d'outil, outils intégrés, adaptateur MCP
  providers/      API (openrouter, anthropic, openai), cli_model, cli_agent, routeur
  guardian/       pare-feu, politiques, moniteur, coffre, outbox, approbations
  channels/       inbox, imessage + line (inkbox), telegram, terminal
  voice/          passerelle Pipecat, outils vocaux, brief de sonnerie
  federation/     identité, relations, capacités, négociateur, mandats, reçus, A2A
  exec/           sandboxes d'exécution
  observability/  logs structurés, traces
  config/         configuration typée, secrets (références seulement)
evals/            jeux d'évaluation (mémoire, proactivité, sécurité, collaboration)
tests/
docs/
```

---

## 15. Ordre de construction

Chaque jalon produit quelque chose d'**utilisable** et de **testé** avant de passer au suivant.

| Jalon | Contenu | Tu peux… |
|---|---|---|
| **M0 — Fondations** | Projet `uv`, CI (lint, types, tests), configuration typée, journal + HLC + projections, logs structurés, superviseur | — (socle) |
| **M1 — Premier cerveau** | Fournisseur OpenRouter, boucle d'agent, contrat d'outil, compilateur de contexte v0, canal terminal, gardien v0 (politiques simples + outbox), budgets | Discuter avec Orion dans un terminal, avec quelques outils sûrs |
| **M2 — Mémoire v0** | Faits bi-temporels, personnes, **registre des engagements**, profil Markdown, recherche plein texte, extraction en arrière-plan, premières évaluations mémoire | Lui apprendre des choses, qu'il s'en souvienne et les corrige |
| **M3 — iMessage + initiative v0** | Canal Inkbox, approbations par iMessage, moteur d'initiative (rappels d'engagements, 3 résumés/jour, heures calmes, « pourquoi maintenant ») | **Lui écrire par iMessage et qu'il t'écrive au bon moment** |
| **M4 — Fournisseurs étendus** | CLI-modèle (`claude`, `codex`), CLI-agent, routeur, vérificateur sur un autre modèle | Utiliser tes abonnements |
| **M5 — Voix** | `orion-voice` (Pipecat + Twilio + GPT-Live), outils à deux vitesses, niveaux d'identité, résumé post-appel | **L'appeler, et qu'il t'appelle quand c'est important** |
| **M6 — Mémoire v1** | Vecteurs + graphe, consolidation de nuit, contradictions, oubli par source vérifié, mail et agenda comme sources | Un vrai second cerveau sur ta vie |
| **M7 — Fédération** | Identité, A2A, relations, négociateur isolé, premier cas : organiser un rendez-vous entre deux Orion | Que ton Orion s'organise avec celui de quelqu'un d'autre |

L'**évaluation** commence dès M1 et grossit à chaque jalon : chaque bug réel devient un cas de test.

---

## 16. Questions ouvertes

1. **Hébergement du cerveau** : quel serveur (VPS existant ? fournisseur préféré ?), quelle région
   (UE conseillée pour la voix et le RGPD) ?
2. **Budget mensuel LLM** visé, pour calibrer le routeur et les budgets par défaut.
3. **Personnalité d'Orion** : nom affiché, ton (tutoiement, humour, concision), à écrire dans la
   constitution.
4. **Comptes à créer** quand on arrivera aux jalons concernés : OpenRouter (M1), Inkbox (M3), Twilio
   (M5).
5. **Langue du code** : je propose code, noms et commentaires en anglais, documentation en français.

---

## 17. Ce qui est construit (au 20/10/2026, jusqu'à D84)

Le plan des sections 1 à 16 tient dans ses principes : journal append-only, projections
reconstructibles, provenance et confiance, gardien séparé, initiative gouvernée, cache de prompt.
Le code s'en est écarté sur la forme, chaque fois par une décision.

### 17.1 Écarts au plan

| Plan | Ce qui existe | Décision |
|---|---|---|
| Cinq processus (`guardian`, `core`, `voice`, `exec`, `negotiator`) | Deux : `orion guardian` (secrets, effets, canaux) et `orion serve` (le cœur, la seule clé du modèle). Deux services systemd sous deux utilisateurs | D28, D30 |
| Voix par Pipecat et Twilio | Voix par Inkbox : Orion répond lui-même au téléphone, avec sa mémoire et ses outils | D23, D40 |
| Trois processus cognitifs | Une boucle d'agent (`cognition/loop.py`), des travaux de fond (extraction, nuit, réflexion) et un moteur d'initiative à règles | D20, D27, D43 |
| Contexte recompilé à chaque appel | La mémoire voyage dans les messages, jamais dans le message système : chaque requête prolonge la précédente octet pour octet | D17, `tests/test_runtime_cache.py` |
| CLI-modèle (`claude`, `codex`) comme fournisseurs | OpenRouter, le forfait ChatGPT du propriétaire (API Responses officielle), et la délégation au CLI `claude` pour les tâches longues | D22, D62 |
| Exécuteurs en bac à sable | Un navigateur isolé qu'Orion conduit, des tâches `computer`, et `orion hand` sur les appareils du propriétaire | D33, D41, D42 |
| Capacités Biscuit, négociateur isolé | Fédération v0 : invitation à usage unique, messages signés Ed25519, créneaux libres seuls divulgués, échangés par le gardien | D29 |
| Arborescence monolithique (§14) | Une plaque et des modules (§17.2) | D24 |

### 17.2 La plaque et les modules

Le cœur garde le journal, la mémoire, le manuel, le gardien, la boîte de réception unifiée,
l'initiative et les rappels. Tout le reste est un module (`orion.modules.base.Module`) qui peut
ajouter des consignes à la constitution, des outils, des services supervisés et des comptes
rendus pour l'initiative. Chaque module déclare ce qu'il permettrait s'il était activé
(`offer`, D64). Un outil par domaine, avec des actions (D59).

| Domaine | Modules | Décisions |
|---|---|---|
| Canaux | `imessage`, `telegram`, `voice`, `vocal` (vocaux dans les deux sens), `photos` (tout fichier reçu) | D18, D40, D50, D63, D71, D88 |
| Sources | `calendar`, `mail` (lecture seule stricte, quarantaine, rangement explicite), `web` | D25, D31, D75 |
| Mains | `computer` (navigateur), `booking` (rendez-vous de bout en bout), `files`, `hands`, `delegation`, `expert` (modèle plus fort) | D33, D41, D46, D60, D61, D73 |
| Second cerveau | `library`, `papers` (coffre des papiers), `missions`, `thinking` (sa nuit : il travaille pendant que tu dors), `curiosity` (sa curiosité à lui), `chronicle`, `intentions`, `relations`, `departure` (la veille d'un départ), `places` | D47, D48, D51–D53, D56–D58, D67, D74, D76, D77, D117 |
| Le connaître | `adaptation` (le manuel), `onboarding`, `advice` (ses conseils assumés), `style` (écrire en son nom), `focus` | D21, D34, D65, D68, D70 |
| Les autres | `federation`, `secretary` | D29, D54 |
| Administration | `configure` (réglages par message), `console`, `night` (rangement de la mémoire), `doubt` (la mémoire qui doute) | D27, D39, D72, D83 |

### 17.3 Le socle, renforcé (D78 à D84)

| Besoin | Ce qui le tient | Décision |
|---|---|---|
| Voir et annuler tout ce qu'Orion fera seul | `Module.planned`, l'outil `planned` | D78 |
| Ne pas dépenser sans limite | `providers.spending` : chaque appel journalisé, le fond coupé au plafond, jamais le premier plan | D79 |
| Rejouer un journal ancien | une version par événement, `core.upcast` à la lecture | D80 |
| Prouver que tout se reconstruit | chaque test rejoue chaque projection ; une sauvegarde est relue en entier | D81 |
| Régler l'arbitre sur des chiffres | `orion simulate`, sur une copie, sans modèle | D82 |
| Ne pas affirmer ce qui a changé | `memory.doubt` : fraîcheur des faits de situation | D83 |
| Garder le début fixe des requêtes court | sections ≤ 2 500 caractères, budgets testés | D84 |

### 17.4 Arborescence réelle

```
src/orion/
  core/          journal, HLC, événements et leurs versions, projections, superviseur, sauvegarde
  cognition/     boucle d'agent, vérificateur
  context/       compilateur, fenêtre récente, résumé, fils
  memory/        faits, personnes, recherche par le sens, extraction, nuit, provenance
  initiative/    moteur, arbitre, minuteurs, ce qui est prévu, simulation
  guardian/      politiques, autonomie, limites, outbox, sessions prêtées, coffre, service
  providers/     openrouter, chatgpt (+ connexion), routeur, dépense
  channels/      boîte de réception unifiée, iMessage (ligne Inkbox), Telegram, webhook, terminal
  modules/       la plaque : base, registre (intégrés puis paquets `orion.modules`)
  tools/         contrat d'outil, outils intégrés
  resources/     consignes de la constitution, une par module
  <module>/      un dossier par module de §17.2
evals/           évaluations sur le vrai modèle (`python -m evals`)
deploy/          installation sur un VPS : Caddy, systemd, sauvegardes
```

### 17.5 Les jalons

| Jalon | État |
|---|---|
| M0 à M3 | Faits |
| M4 | Fait, sauf le CLI comme modèle principal (remplacé par le forfait ChatGPT, D62) |
| M5 | Fait, par Inkbox au lieu de Pipecat (D23, D40) |
| M6 | Fait : mémoire v1, agenda et mail en lecture puis en écriture approuvée |
| M7 | v0 : deux Orion calent un rendez-vous (D29) |

Au-delà du plan : missions, bibliothèque, chronique, intentions, secrétaire, papiers,
rendez-vous, rangement du mail, carnet d'erreurs, curiosité, veille des départs.
