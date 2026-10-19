# Étape 1 — Anatomie de Claude Code

> Objectif : comprendre en profondeur comment Claude Code fonctionne, **pourquoi** il est construit
> ainsi, et quelles hypothèses implicites il porte — pour ensuite décider lesquelles garder et
> lesquelles casser.
>
> Date de l'étude : 19 octobre 2026 (Claude Code ≈ v2.1.30x, modèles 5.5).

---

## 0. Méthode et fiabilité des sources

Quatre recherches parallèles, puis recoupement :

| Source | Fiabilité | Remarque |
|---|---|---|
| Docs officielles `code.claude.com/docs` | ★★★ | Décrit le comportement actuel, pas l'implémentation |
| Blog ingénierie Anthropic (2024 → 2026) | ★★★ | Le *pourquoi* : principes, chiffres, erreurs passées |
| Fuite des source maps npm v2.1.88 (mars 2026) — `claude-code-from-source`, `learn-coding-agent`, arXiv 2604.14228 | ★★☆ | Code réel mais **figé à v2.1.88** |
| Interception de trafic (Shatrov, Yuyz0112, Sung, MinusX) | ★★☆ | Ce qui part réellement au modèle, à une date donnée |
| Extraction des prompts par version (`Piebald-AI/claude-code-system-prompts`) | ★★★ | Texte exact des prompts, à jour |
| Analyses « LLM lisant du JS minifié » (shareAI, Southbridge) | ★☆☆ | Constantes parfois justes, prose souvent inventée |

Règle appliquée : un chiffre interne n'est retenu que s'il est confirmé par au moins deux sources
ou par la doc officielle. Les points incertains sont marqués **[incertain]**.

---

## 1. Vue d'ensemble : une boucle triviale, un harnais énorme

La phrase qui résume tout (analyse arXiv de la fuite) : **≈ 1,6 % du code est de la logique
« IA », ≈ 98 % est du harnais opérationnel** — permissions, contexte, reprise sur erreur,
persistance, interface.

```
                ┌───────────────────────────── Interfaces ─────────────────────────────┐
                │ TUI (React/Ink forké) · headless -p · Agent SDK · Web/Cloud · Remote  │
                └───────────────────────────────────┬──────────────────────────────────┘
                                                    │ messages / file d'attente
┌────────────── Contexte ───────────────┐   ┌───────▼────────┐   ┌──────── Exécution ────────┐
│ system prompt statique (cache global) │   │                │   │ Outils (≈40, contrat typé) │
│ ── frontière dynamique ──             │──▶│  queryLoop()   │──▶│ Permissions (6 couches)    │
│ CLAUDE.md, mémoire, env, skills list  │   │  while(true)   │   │ Hooks (≈33 événements)     │
│ conversation + <system-reminder>      │◀──│                │◀──│ Sandbox OS (bwrap/seatbelt)│
│ pipeline de compaction (5 couches)    │   └───────┬────────┘   └────────────────────────────┘
└───────────────────────────────────────┘           │
                ┌───────────────────────────────────▼──────────────────────────────────┐
                │ Délégation : sous-agents · forks · équipes · workflows               │
                │ Durée : sessions JSONL · checkpoints · cron/loop · monitors · daemon │
                └──────────────────────────────────────────────────────────────────────┘
```

Philosophie revendiquée par Anthropic : **« give your agents a computer »**. Pas de graphe
d'états, pas de planificateur imposé, pas de RAG : le modèle décide, le harnais encadre.

---

## 2. La boucle d'agent

### Mécanique (confirmée docs + fuite)

1. Le modèle reçoit system prompt + outils + historique.
2. Il répond par du texte et/ou des blocs `tool_use`.
3. Le harnais exécute les outils (hooks → permissions → exécution) et renvoie des `tool_result`.
4. On recommence. **La boucle s'arrête quand le modèle répond sans appel d'outil.**

Dans la v2.1.88, c'est `queryLoop()`, un `async function*` en `while(true)`. Le choix d'un
**générateur** plutôt que d'un émetteur d'événements donne de la *backpressure* : la boucle se met
en pause si l'interface ou le client SDK ne consomme pas.

Séparation d'état notable :
- `State` mutable (compteur de tours, suivi de compaction…),
- `QueryConfig` immuable, figé à l'entrée (feature flags lus une seule fois),
- `QueryDeps` injectables (appel modèle, compacteur, générateur d'UUID) → testabilité.

La boucle renvoie une **raison de terminaison typée** (`max_turns`, `prompt_too_long`,
`reactive_compact_retry`, …), pas un simple booléen.

### Exécution des outils

- **Exécution spéculative** : un `tool_use` est lancé dès qu'il est entièrement parsé dans le flux,
  pendant que le modèle continue d'écrire.
- **Partition en lots** en conservant l'ordre : un lot est soit « tous sûrs en concurrence », soit
  « un seul outil sériel ». Ex. `[Read, Read, Grep] [Edit] [Read]`. Concurrence max 10.
- La sûreté dépend **de l'entrée**, pas seulement de l'outil : `Bash("ls && cat x")` est
  parallélisable, `Bash("rm x")` non.
- Les modifications de contexte d'un lot parallèle sont appliquées **dans l'ordre des outils, pas
  dans l'ordre de fin** → déterminisme.
- Trois niveaux d'annulation (requête → lot → outil). Seule une erreur Bash annule les outils
  frères.

### Reprise sur erreur (fuite v2.1.88)

- Plafond de sortie à 8K tokens → escalade à 64K → jusqu'à 3 tentatives de reprise multi-tours.
- Erreur 413 (prompt trop long) → vidage de la « context collapse » → **une seule** compaction
  réactive.
- Les erreurs récupérables sont **retenues** hors du flux tant que la reprise n'a pas échoué.
- Disjoncteurs partout : 3 échecs de compaction consécutifs ⇒ arrêt. Ajouté après des sessions
  qui brûlaient ~250 000 appels API par jour en boucle d'échec.

### Pilotage en cours de route

- Un message tapé pendant que l'agent travaille est **mis en file** et injecté dès que les outils
  en cours finissent, *dans le même tour*.
- `Esc` interrompt l'outil courant ; `Ctrl+Enter` force l'envoi.
- `/btw` : question annexe qui voit tout le contexte, sans outils, sans entrer dans l'historique.
- `/goal <condition>` : après chaque tour, **un modèle évaluateur séparé** vérifie la condition ;
  si elle n'est pas remplie, un nouveau tour démarre automatiquement.

---

## 3. Le système d'outils

### Contrat d'un outil (fuite v2.1.88, deux sources concordantes)

```
buildTool({
  name, aliases,
  inputSchema,                // Zod
  prompt() / description(),   // texte vu par le modèle
  validateInput(input),       // validation sémantique (ex. refuser un Edit sans effet)
  checkPermissions(input),
  call(input, ctx),           // async generator → progression
  mapToolResultToAPI(),
  isReadOnly(input), isConcurrencySafe(input), isDestructive(),
  interruptBehavior(), shouldDefer, maxResultSizeChars,
  toAutoClassifierInput(),    // ce que voit le classifieur de sécurité
  render*()                   // rendu TUI
})
```

**Valeurs par défaut fermées** (fail-closed) : `isReadOnly = false`,
`isConcurrencySafe = false`. Un outil oublié est traité comme dangereux et sériel.

### Pipeline d'exécution (14 étapes)

Lookup (avec alias pour les vieux transcripts) → abandon ? → validation Zod (avec indice
« appelle ToolSearch » si l'outil est différé) → `validateInput` → démarrage spéculatif du
classifieur → normalisation de l'entrée (`~` étendu) → hooks PreToolUse → décision de permission →
gestion du refus → `call()` → budget du résultat → hooks PostToolUse → messages additionnels →
télémétrie.

### Budgets de sortie

| Outil | Limite inline | Au-delà |
|---|---|---|
| Bash | ~30 000 car. | écrit sur disque, aperçu de 2 000 car. + chemin |
| Grep | `head_limit` 250 par défaut | pagination par `offset` |
| Read | 25K tokens | « vue PARTIELLE », pagination |
| MCP | avertissement 10K, plafond 25K tokens | fichier sur disque au-delà de 50K car. |

Un **budget global de conversation** s'ajoute aux budgets par outil.

### Outils notables

- Il n'y a **plus de Glob/Grep par défaut** sur Linux/macOS : `find`/`grep` via Bash
  (bfs/ugrep embarqués) suffisent.
- `Edit` = remplacement exact et unique ; refusé si le fichier a changé depuis la dernière lecture.
- Un `Bash` qui dépasse son délai **passe en arrière-plan** au lieu d'être tué.
- `Monitor` : chaque ligne de sortie d'une commande de fond devient **un événement injecté** dans
  la conversation. C'est une primitive réactive.
- `ToolSearch` : les outils MCP ne sont chargés que par leur nom ; le schéma arrive à la demande.
  Gain mesuré par Anthropic : ~77K → ~8,7K tokens de contexte, précision 49 % → 74 %.

---

## 4. Contexte et cache : la vraie contrainte d'architecture

### Composition du contexte au démarrage (fenêtre de 200K)

| Élément | ≈ tokens |
|---|---|
| System prompt + schémas des outils intégrés | 4,2K |
| Index de mémoire auto (`MEMORY.md`, 200 lignes / 25 Ko max) | 0,7K |
| Environnement (cwd, OS, git…) | 0,3K |
| Outils MCP différés (noms seulement) | 0,1K |
| Descriptions des skills (1 ligne chacune, budget 1 % de la fenêtre) | 0,5K |
| CLAUDE.md utilisateur + projet | 2K |

Tout le reste arrive **juste à temps** : fichiers, corps des skills, schémas d'outils.

### Le cache de prompt dicte la structure

Le cache est une **correspondance exacte de préfixe**. Un changement dans une couche invalide tout
ce qui suit. D'où l'ordre :

```
[system prompt statique]  ← caché en portée GLOBALE (partagé entre tous les utilisateurs)
── SYSTEM_PROMPT_DYNAMIC_BOUNDARY ──
[mémoire, env, instructions MCP, style]
[conversation]
[nouveaux éléments ajoutés en fin via <system-reminder>]
```

Conséquences de conception :
- Chaque condition placée avant la frontière **double** le nombre de variantes du cache. La partie
  statique est donc gardée sans condition.
- Les informations dynamiques (fichier modifié, état des tâches, sortie de hook) sont **ajoutées à
  la fin** sous forme de `<system-reminder>`, jamais réinsérées plus haut. On en compte environ 155
  types dans v2.1.303.
- La liste d'outils est **gelée** pour toute la conversation.
- CLAUDE.md est injecté dans le **premier message utilisateur**, pas dans le system prompt
  (observé dans le trafic).
- Les forks de sous-agents réutilisent le préfixe exact du parent : environ 95 % d'économie sur
  l'entrée **[incertain : chiffre de la fuite]**.
- Le bug du postmortem d'avril 2026 : un nettoyage du raisonnement censé s'exécuter une seule fois
  s'exécutait à chaque tour. Résultat : Claude « oubliait » et le cache ratait systématiquement.

### Pipeline de compaction (du moins cher au plus cher)

1. **Budget** : les gros résultats d'outils partent sur disque.
2. **Snip** : suppression physique de vieux messages, avec un marqueur de frontière.
3. **Microcompaction** : les anciens résultats d'outils sont remplacés par
   `[Old tool result content cleared]`. Version temporelle : après environ 60 min d'inactivité, le
   cache est froid de toute façon, donc on nettoie **[incertain : source unique]**. Version
   « cache edits » : suppression côté serveur sans casser le préfixe.
4. **Context collapse** : des segments de conversation sont remplacés par des résumés projetés à
   la lecture.
5. **Auto-compaction** : un appel modèle complet résume tout.
   - Déclenchement (v2.1.88) : `fenêtre − min(sortie max, 20K) − 13K`, soit environ 83 % sur 200K.
     Aujourd'hui sur 1M : environ 967K.
   - Prompt de résumé (texte exact connu) : d'abord un brouillon `<analysis>` chronologique, puis
     un `<summary>` en **9 sections**. Les sections : intention, concepts, fichiers et extraits de
     code, erreurs et corrections, résolution, **tous les messages utilisateur**, tâches en
     attente, travail en cours, prochaine étape avec citations.

**Ce qui survit à une compaction :** le system prompt ; CLAUDE.md racine et la mémoire, relus sur
disque ; le plan ; un statut git frais ; les **5 derniers fichiers** touchés ; les corps de skills
invoqués (5K chacun, 25K au total) ; les tâches de fond.

**Ce qui se perd :** les règles à portée de chemin (rechargées à la demande), le contexte ajouté
par les hooks (résumé) et **les limites posées oralement** (« ne pousse pas ») — un défaut reconnu
par la doc.

Anthropic a mesuré, sur sa recherche agentique interne :
- nettoyage des résultats d'outils seul : +29 % ;
- avec mémoire en plus : +39 % ;
- −84 % de tokens sur une évaluation de 100 tours.

---

## 5. Mémoire

Deux systèmes, chargés à chaque session. Ce sont **du contexte, pas des garanties** : pour imposer
un comportement, on utilise des hooks ou des règles.

### CLAUDE.md (instructions écrites par l'humain)

- Hiérarchie : entreprise → utilisateur → projet → local. Les fichiers sont **concaténés**, le
  plus spécifique est lu en dernier.
- Les CLAUDE.md des sous-dossiers ne sont chargés **que lorsque l'agent touche un fichier dans ce
  dossier**.
- `@chemin` pour les imports (4 niveaux max). `.claude/rules/*.md` avec `paths:` en frontmatter :
  la règle n'est chargée que si un fichier correspondant est lu ou édité.
- Cible recommandée : moins de 200 lignes. Test d'Anthropic : *« Est-ce que supprimer cette ligne
  ferait faire une erreur à Claude ? Sinon, coupe. »*

### Mémoire automatique (notes écrites par l'agent)

- `~/.claude/projects/<repo>/memory/` contient un index `MEMORY.md` (une ligne par souvenir, seuls
  les 200 premières lignes / 25 Ko sont chargés) et un fichier par souvenir.
- 4 types : `user`, `feedback`, `project`, `reference`. Horodatage `modified`.
- Ne stocke pas ce qui est **dérivable** (code, git) ni ce qui est déjà dans CLAUDE.md.
- Rappel (fuite v2.1.88) : un **appel latéral à Sonnet** reçoit le manifeste (frontmatter des
  fichiers), la requête et les outils récents, choisit quels souvenirs charger, et ce choix est
  préchargé en asynchrone.
- Les sous-agents peuvent avoir leur propre mémoire persistante (`memory: user|project|local`).

### Skills (procédures)

Divulgation progressive à 3 niveaux :
1. nom + description toujours en contexte ;
2. corps chargé à l'invocation ;
3. fichiers et scripts annexes lus ou exécutés seulement si le corps y renvoie.

La taille totale d'un skill est donc pratiquement illimitée. Les scripts s'exécutent **sans entrer
dans le contexte**. Anthropic décrit les skills comme le moyen pour l'agent de *« codifier ses
propres comportements en capacités réutilisables »*.

---

## 6. Délégation

| Mécanisme | Contexte | Retour au parent | Usage |
|---|---|---|---|
| **Sous-agent** | Neuf : son prompt + la consigne + CLAUDE.md + statut git | Rapport final seul | Isoler une exploration bruyante |
| **Fork** | Copie exacte du parent (même cache) | Rapport final | Paralléliser à bas coût |
| **Équipe** (expérimental) | Instances séparées | Liste de tâches partagée + boîtes aux lettres | Coordination entre pairs |
| **Workflow** | Script qui orchestre des agents | Résultat du script | Orchestration déterministe |

Détails clés :
- Le parent choisit un sous-agent d'après sa `description`.
- Les rapports sont **filtrés** : un texte qui ressemble à une instruction est neutralisé, et le
  rapport est marqué « sans autorité utilisateur ».
- Profondeur max 3, 20 sous-agents simultanés. Explore et Plan sont en lecture seule, **sans
  CLAUDE.md** (économie revendiquée de « milliards de tokens par semaine »).
- En arrière-plan, le résultat arrive plus tard sous forme de notification.

Leçons Anthropic sur le multi-agent :
- Le multi-agent coûte environ **15×** les tokens d'une conversation.
- Il bat un agent seul de 90 % en recherche, mais il est mauvais pour le travail à contexte
  partagé, comme le code.
- 80 % de la variance de performance s'explique par… **le nombre de tokens dépensés**.
- Les sous-agents doivent **écrire leurs résultats dans des fichiers et renvoyer des références**,
  pour éviter le « téléphone arabe ».

---

## 7. Extensibilité

| Primitive | Nature | Déterministe ? |
|---|---|---|
| **Hooks** (~33 événements, 5 types : commande, HTTP, outil MCP, prompt LLM, agent) | Code branché sur le cycle de vie | Oui (sauf types prompt et agent) |
| **Skills** | Savoir-faire chargé à la demande | Non (consultatif) |
| **Sous-agents** | Rôles avec contexte isolé | Non |
| **MCP** | Protocole standard d'outils, ressources, prompts, push (« channels ») | — |
| **Plugins** | Paquet de tout ce qui précède + marketplace | — |
| **Output styles** | Remplacement de la personnalité et du rôle | — |
| **Agent SDK** | Le même binaire en bibliothèque : `query()`, outils in-process, hooks en callbacks, `canUseTool` | — |

Événements de hook les plus intéressants pour nous :
- `PreToolUse` : peut `allow`, `deny`, `ask` ou `defer`, et réécrire l'entrée.
- `PostToolUse` : peut réécrire la sortie.
- `Stop` : peut **refuser l'arrêt** et forcer la poursuite (8 fois max d'affilée).
- `UserPromptSubmit`, `SessionStart` : injectent du contexte.
- `FileChanged` : surveillance de fichiers.
- `PreCompact` / `PostCompact`.
- `asyncRewake` : un hook de fond qui **réveille l'agent même inactif**.

---

## 8. Sécurité

### Couches de décision (dans l'ordre)

1. Hooks PreToolUse.
2. Règles `deny` → `ask` → `allow` : la première qui correspond gagne, et un deny bat tout.
3. `checkPermissions()` propre à l'outil.
4. Mode : `default`, `acceptEdits`, `plan`, `auto`, `dontAsk` ou `bypassPermissions`.
5. Demande à l'humain.
6. **Classifieur du mode auto.**

### Le mode auto (mode par défaut en interactif depuis v2.1.283)

- Point de départ : les utilisateurs approuvent **93 %** des demandes, ce qui est une fatigue
  d'approbation.
- Niveaux :
  1. lecture et règles étroites : autorisé ;
  2. éditions dans le projet (réversibles via git) : autorisé ;
  3. tout le reste : classifieur à 2 étages. Un filtre rapide d'un token, puis un raisonnement
     seulement pour ce qui a été signalé.
- **Le classifieur est « aveugle au raisonnement »** : il ne voit que les messages utilisateur et
  les appels d'outils, ni la prose de l'agent ni les résultats d'outils. L'agent ne peut donc pas
  le convaincre, et une injection dans une page web ne l'atteint pas.
- Principe : *« tout ce que l'agent choisit de lui-même est non autorisé tant que l'utilisateur ne
  l'a pas dit »*. « Peut-on corriger ça ? » est une question, pas un ordre.
- **Refuser et continuer** : le refus revient à l'agent comme résultat d'outil. Après 3 refus
  consécutifs ou 20 au total, on repasse la main à l'humain.
- Résultats : 0,4 % de faux positifs, mais **17 % de faux négatifs** sur les actions trop zélées.
  C'est insuffisant pour l'irréversible.

### Confinement : la couche la plus fiable

- Sandbox OS : bubblewrap + espace de noms réseau sous Linux, Seatbelt sous macOS. Écriture limitée
  au dossier de travail ; réseau uniquement via un proxy avec liste de domaines autorisés.
- **Il faut les deux barrières.** Le système de fichiers seul laisse fuir par le réseau ; le
  réseau seul laisse s'échapper du système de fichiers.
- **Les identifiants n'entrent jamais dans la sandbox** : un proxy hors sandbox valide l'opération
  (ex. pousser seulement sur la bonne branche) puis ajoute le vrai jeton.
- Incidents publiés :
  - exfiltration de `~/.aws/credentials` dans 24 tentatives sur 25 lors d'un red-team par
    hameçonnage ;
  - domaine autorisé détourné pour téléverser des fichiers. *« Une liste de domaines autorisés est
    une attribution de capacité, pas un filtre de destination. »*
- Leçon : contenir d'abord, surveiller ensuite. Préférer les primitives standard aux proxys faits
  maison.

---

## 9. Durée et proactivité

C'est le domaine où Claude Code est **le moins abouti pour un Jarvis**. Les primitives existent
mais elles sont dispersées :

| Primitive | Ce qu'elle fait | Limite |
|---|---|---|
| Sessions JSONL + `--resume` | Persistance de l'historique | Format interne et instable |
| Checkpoints / `/rewind` | Annuler code et conversation (100 derniers) | Ne suit pas Bash ni les effets externes |
| `/loop`, cron, `ScheduleWakeup` | Réveils planifiés dans une session | **Ne se déclenchent qu'entre deux tours**, expirent après 7 jours, sans rattrapage |
| `Monitor` | Un flux devient des événements dans la conversation | 30 min max |
| Channels MCP | Telegram, Discord, webhooks poussent dans la session | Aperçu de recherche |
| Routines (cloud) | Planification, appel API ou événement GitHub → nouvelle session | Fréquence min. 1 h, contexte neuf à chaque fois |
| Démon superviseur | Sessions de fond qui survivent au terminal | Ramasse les sessions inactives après ~1 h |
| `/goal`, hook Stop | Empêche l'arrêt tant qu'une condition n'est pas remplie | Lié à une session |

Le **modèle mental reste « une session = une conversation lancée par un humain »**. La
proactivité y est greffée.

### Le motif d'Anthropic pour les tâches longues

Il vient de deux billets : « Effective harnesses » (nov. 2025) et « Harness design » (mars 2026).

- Un **agent initialisateur** crée :
  - une liste de fonctionnalités en **JSON** (le modèle la modifie moins à tort que du Markdown),
    toutes à `passes: false` ;
  - un fichier de progression ;
  - un script de démarrage ;
  - un premier commit.
- Chaque session suivante suit un **rituel** :
  1. `pwd`, puis lire le log git et le fichier de progression ;
  2. lancer un test de fumée **avant** de toucher quoi que ce soit ;
  3. traiter **une seule** fonctionnalité ;
  4. committer, puis mettre à jour la progression.
- **Générateur ≠ évaluateur.** *« Un évaluateur sceptique est plus facile à régler qu'un
  générateur autocritique. »* Avant chaque sprint, un **contrat** définit ce que « fini » veut dire
  et comment on le vérifie.
- **Managed Agents** (avril 2026) découpe le système en trois parties, comme un OS :
  - la **session** est un journal d'événements append-only, durable, la seule source de vérité ;
  - le **harnais** est sans état ;
  - la **sandbox** est jetable.
  Interfaces : `wake(sessionId)`, `getEvents()`, `execute(name, input)`. *« Le cerveau est découplé
  des mains. »*

---

## 10. Les principes d'Anthropic, condensés

1. **Simple d'abord.** Une boucle d'outils. On n'ajoute de la complexité que si une évaluation la
   justifie.
2. **Chaque composant du harnais est une hypothèse datée** sur une faiblesse du modèle. On les
   retire un par un à chaque nouveau modèle. Les remises à zéro du contexte, les sprints et l'outil
   « think » sont tous devenus inutiles.
3. **Le contexte est un budget d'attention.** Le plus petit ensemble de tokens à fort signal ;
   chargement juste à temps par identifiants (chemins, requêtes) ; l'arborescence de dossiers est
   elle-même du contexte.
4. **La recherche agentique bat le RAG** : grep et lecture incrémentale, avec les embeddings en
   option seulement.
5. **Des outils pensés pour des intentions, pas des API.** `schedule_event` plutôt que trois
   appels ; noms lisibles plutôt qu'UUID ; option `response_format` ; erreurs actionnables ;
   exemples d'usage (précision 72 % → 90 %).
6. **Code plutôt qu'appels d'outils en chaîne** : MCP exposé comme une API de code donne
   150K → 2K tokens, et les données personnelles ne passent pas par le modèle.
7. **Vérification exécutable**, avec des preuves plutôt que des affirmations. L'issue d'une tâche
   est **l'état réel du monde**, pas ce que l'agent dit.
8. **Évaluer en pass^k, pas en pass@k** : pour un agent qui agit en ton nom, la constance compte
   plus que la réussite occasionnelle. Commencer avec 20 à 50 cas réels.
9. **Le moindre changement de prompt se mesure.** Une seule ligne sur la verbosité a coûté environ
   3 % en code.
10. **Superviser, ne pas tout approuver.** Un flux d'activité lisible, des interruptions faciles,
    et un agent qui sait dire qu'il doute.

---

## 11. Les hypothèses implicites de Claude Code — et où on peut les casser

C'est le cœur de l'étape. Pour chaque hypothèse : ce que Claude Code suppose, pourquoi c'est
raisonnable pour lui, et pourquoi ça ne l'est pas pour un Jarvis.

### H1. « Le travail commence par un message humain. »
- **Claude Code :** la session et le tour sont déclenchés par l'utilisateur. La proactivité est
  rajoutée par-dessus (cron entre deux tours, routines, channels).
- **Pour un Jarvis :** l'unité de base n'est pas la conversation mais un **flux d'événements
  continu**. Un message de toi est un événement parmi d'autres (mail, calendrier, fichier modifié,
  échéance, absence de nouvelles). L'agent tourne en permanence et **décide** quand réfléchir,
  agir ou se taire.
- **Piste :** un noyau « événement → attention → décision » où la conversation n'est qu'une
  interface.

### H2. « Le contexte est une conversation qu'on compresse quand elle déborde. »
- **Claude Code :** l'historique est la mémoire de travail. Cinq couches de compression le
  rognent, chaque compaction est **irréversible et avec perte**, et les limites dites à l'oral
  disparaissent.
- **Ce que dit Anthropic lui-même :** Managed Agents sort déjà le journal du contexte (« la session
  n'est pas la fenêtre de contexte »).
- **Piste :** **le contexte est recompilé à chaque appel** à partir d'un état structuré : objectifs
  actifs, engagements, faits pertinents, tâche en cours, preuves récentes. Le journal brut reste
  consultable mais n'est jamais « la mémoire ». Plus de compaction, une **projection**.

### H3. « La mémoire, ce sont des notes que l'agent relit. »
- **Claude Code :** un index Markdown plus des fichiers, avec un rappel choisi par un appel
  latéral. Rien ne consolide, ne vérifie ni ne fait expirer les souvenirs.
- **Pour un second cerveau :** la mémoire doit être **vivante**. Elle consolide les épisodes en
  faits, détecte les contradictions, porte une provenance et une confiance, se périme, et pose des
  questions quand elle doute.
- **Piste :** une mémoire typée avec un **processus de fond** qui la travaille pendant que tu
  dors : consolidation, liens, oubli actif.

### H4. « Une session a un seul fil de pensée. »
- **Claude Code :** une boucle unique. Les sous-agents sont des parenthèses qui renvoient un
  rapport.
- **Piste :** plusieurs **processus cognitifs** à rythmes différents : un réflexe rapide et bon
  marché pour trier les événements, un raisonnement profond à la demande, une réflexion lente
  périodique (bilan, planification). Chacun a son modèle, son budget et son contexte.

### H5. « La sécurité, c'est filtrer des actions. »
- **Claude Code :** règles, classifieur et sandbox. Excellent, mais **sans état** : la confiance
  ne s'apprend pas.
- **Piste :**
  - une **confiance par capacité, gagnée et révocable**, fondée sur l'historique des actions et
    les corrections que tu as faites ;
  - une **simulation avant exécution** (aperçu du diff de l'effet réel : « voici le mail qui
    partira ») ;
  - une **annulation universelle** des effets externes, avec brouillons, délais de grâce et
    compensations.
  Les checkpoints de Claude Code ne couvrent que les fichiers.

### H6. « L'humain surveille l'écran. »
- **Claude Code :** l'humain est devant le terminal, et les notifications sont un bonus.
- **Pour un Jarvis :** tu n'es **pas** là la plupart du temps. Ton attention est la ressource la
  plus rare du système.
- **Piste :** un **budget d'interruption** explicite. L'agent regroupe, priorise, choisit le canal
  et le moment, et apprend ce qui mérite de te déranger. Ce sujet n'est traité par aucun harnais
  existant.

### H7. « Le harnais est codé, les comportements sont promptés. »
- **Claude Code :** les skills sont écrits par des humains. L'agent peut en écrire, mais rien ne
  les **valide**.
- **Piste :** les procédures apprises sont des **candidates** qui ne sont promues qu'après avoir
  réussi des évaluations rejouables (pass^k). Une **auto-amélioration contrôlée** : l'agent propose,
  le banc d'essai tranche.

### H8. « Une tâche est finie quand le modèle s'arrête. »
- **Claude Code :** la boucle s'arrête quand il n'y a plus d'appel d'outil. `/goal` et le hook
  Stop corrigent en partie ce point.
- **Piste :** une tâche n'est finie que quand **son contrat de vérification passe**, ce contrat
  étant défini *avant* le travail et évalué par un vérificateur distinct. Le livrable, c'est la
  **preuve**, pas l'affirmation.

### H9. « Un utilisateur, une machine, un dépôt. »
- **Claude Code :** le cache, la mémoire et les permissions sont liés à un dossier ou à un dépôt
  git.
- **Pour un Jarvis :** le périmètre, c'est **ta vie**. Projets, personnes, finances, santé,
  agenda. La structure de la mémoire et des permissions doit être organisée **par domaine de vie**,
  pas par répertoire.

---

## 12. Ce qu'on garde tel quel (c'est déjà excellent)

- La **boucle minimale** en générateur, avec des raisons de terminaison typées et des disjoncteurs.
- Le **contrat d'outil** fail-closed, avec une sûreté qui dépend de l'entrée, le parallélisme des
  lectures, la sérialisation des écritures et l'ordre déterministe.
- Des **budgets de sortie** avec débordement sur disque et renvoi d'un chemin.
- La **conception guidée par le cache** : préfixe statique, ajouts dynamiques en fin, outils gelés,
  chargement différé.
- La **divulgation progressive** (skills) et le **juste à temps** (chemins plutôt que contenus).
- Les **hooks déterministes** pour tout ce qui doit être garanti.
- Le **confinement OS** et les **identifiants hors de portée** de l'agent.
- Un classifieur **aveugle au raisonnement** et le principe **refuser et continuer**.
- Le **journal d'événements append-only** comme source de vérité, avec un harnais sans état
  (Managed Agents).
- Le principe **générateur ≠ évaluateur** et les **évaluations pass^k** dès le premier jour.
- MCP comme standard d'interconnexion.

---

## 13. Questions ouvertes pour toi

Elles orienteront les étapes 3 et 4 :

1. **Où vit Orion ?** Sur ta machine, sur un serveur toujours allumé, ou les deux (un cerveau sur
   le serveur, des « mains » sur tes appareils) ?
2. **Quels canaux ?** Terminal, Telegram, voix, application mobile, mail…
3. **Quel périmètre pour le second cerveau ?** Uniquement le travail et le code, ou toute ta vie
   (agenda, mails, finances, santé, notes) ?
4. **Langage ?** L'ancien Orion était en Python. TypeScript a l'écosystème MCP et Agent SDK le plus
   riche. Python est plus confortable pour la donnée et le ML. Rust ou Go pour un démon robuste ?
5. **Fournisseurs de modèles ?** Uniquement Claude, ou multi-fournisseurs (y compris local) ?
6. **Construire au-dessus de l'Agent SDK** (on hérite de la boucle, des outils et du sandbox de
   Claude Code), **ou écrire notre propre noyau** (liberté totale pour casser H1 à H9) ?
   Ma recommandation provisoire : notre propre noyau, car les ruptures visées touchent justement la
   boucle et le contexte, que le SDK impose.

---

## Sources principales

**Officielles**
- Docs : https://code.claude.com/docs/en/how-claude-code-works · /context-window · /prompt-caching
  · /memory · /sessions · /checkpointing · /permissions · /permission-modes · /sandboxing
  · /tools-reference · /hooks · /sub-agents · /agent-teams · /skills · /mcp · /plugins
  · /scheduled-tasks · /routines · /channels · /headless · /agent-sdk/overview · /best-practices
- Ingénierie Anthropic : building-effective-agents · effective-context-engineering-for-ai-agents
  · writing-tools-for-agents · multi-agent-research-system
  · effective-harnesses-for-long-running-agents · harness-design-long-running-apps
  · managed-agents · claude-code-auto-mode · claude-code-sandboxing · how-we-contain-claude
  · advanced-tool-use · code-execution-with-mcp · equipping-agents-for-the-real-world-with-agent-skills
  · demystifying-evals-for-ai-agents · building-c-compiler · april-23-postmortem
  (toutes sous https://www.anthropic.com/engineering/)
- Recherche : https://www.anthropic.com/research/measuring-agent-autonomy · /project-vend-2
  · /prompt-injection-defenses
- https://claude.com/blog/context-management

**Communautaires (non officielles)**
- https://github.com/alejandrobalderas/claude-code-from-source (v2.1.88)
- https://github.com/sanbuphy/learn-coding-agent (v2.1.88)
- https://arxiv.org/abs/2604.14228 — « Dive into Claude Code »
- https://github.com/Piebald-AI/claude-code-system-prompts (prompts exacts par version)
- https://minusx.ai/blog/decoding-claude-code/
- https://kirshatrov.com/posts/claude-code-internals
- https://github.com/Yuyz0112/claude-code-reverse
- https://cc.bruniaux.com/guide/architecture
