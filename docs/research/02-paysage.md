# Étape 2 — Le paysage des agents personnels et l'état de l'art multi-agent

> Objectif : comprendre ce que font les autres agents « toujours actifs » en 2026, où ils
> convergent, où ils échouent, et ce que la recherche dit du travail en équipe d'agents.
> Orion doit faire mieux sur les axes qui comptent pour l'utilisateur : iMessage, voix, vie
> entière, multi-agent.
>
> Date de l'étude : 19 octobre 2026.

---

## 0. Ce qu'on a étudié

| Système | Nature | Source principale |
|---|---|---|
| **Hermes Agent** (Nous Research) | Open source, Python, ~9 900 fichiers | Code source cloné (commit `0a68b24e`) + docs |
| **OpenClaw** (ex-Clawdbot/Moltbot) | Open source, TypeScript, ~15 700 fichiers | Code source cloné (commit `02e11d6`) + docs |
| **OpenAI Dots** | Produit, lancé le 29/09/2026 | Presse, guides tiers (pas d'accès à la doc officielle) |
| **Meta Muse** | Produit, lancé le 08/09/2026 | Newsroom Meta, presse |
| **Grok Bot** (SpaceXAI, ex-xAI) | Produit, bêta depuis le 11/08/2026 | Presse, guides tiers |
| **Poke**, Gemini Spark, Alexa+, Siri… | Produits | Presse, revues |
| **Multi-agent** | Recherche + systèmes de production | Articles (MAST, Who&When, Google/MIT…), blogs Anthropic, Cognition, Cursor, LangChain |

Fiabilité : les deux projets open source sont décrits **depuis leur code**, donc précisément. Les
produits fermés sont décrits depuis la presse et des guides : les détails sont plausibles mais non
vérifiés à la source.

Constat de départ : Grok Bot (août), Muse (8 sept.) et Dots (29 sept.) sont sortis en **sept
semaines**. 2026 est l'année de la vague « agent personnel toujours actif ». Hermes et OpenClaw en
sont les équivalents open source.

---

## 1. La question de l'abonnement Claude — tranchée

C'était la question ouverte D5-bis. Le code de Hermes et d'OpenClaw montre **trois manières**
d'utiliser un abonnement Claude, et elles n'ont pas du tout le même statut.

### Voie A — Réutiliser le jeton OAuth en se faisant passer pour Claude Code ❌

C'est ce que fait la voie « par défaut » de Hermes (`agent/anthropic_adapter.py`) :
- il réutilise l'identifiant client OAuth public de Claude Code, ou « emprunte » les identifiants
  de `~/.claude/.credentials.json` ;
- il envoie `user-agent: claude-code/<version>` et préfixe le prompt système par *« You are Claude
  Code, Anthropic's official CLI for Claude. »* ;
- il **réécrit** « Hermes Agent » → « Claude Code » et « Nous Research » → « Anthropic » dans le
  prompt, et renomme ses outils pour échapper au classifieur d'Anthropic qui détecte les
  applications tierces.

C'est de l'**usurpation d'identité** pour contourner une détection, contraire à la politique
d'Anthropic. Et en pratique, ça ne marche même pas comme espéré : la doc de Hermes reconnaît que
cette voie **ne consomme jamais le forfait de base**, seulement des crédits « extra usage » payants,
et elle produit un flot de bugs (erreurs 400, jetons révoqués, jetons de Claude Code invalidés).
OpenClaw fait la même chose (identité client Claude Code « maintenue ») et recommande les clés API
en production.

### Voie B — Piloter le binaire `claude` officiel comme un simple « modèle » ✅

C'est le plugin officiel **`claude-subscription-directsdk`** de Hermes, et c'est la découverte
importante de l'étape :
- à chaque appel modèle, Hermes lance le **binaire `claude` officiel, non modifié**, en
  `stream-json` avec `--max-turns 1` ;
- les outils natifs de Claude Code sont désactivés ; les outils de Hermes sont **annoncés via MCP**
  mais leur exécution native est refusée : c'est Hermes qui les exécute lui-même ;
- un relais local garantit qu'une seule requête part par appel ;
- **le plugin ne touche jamais aux identifiants** : l'authentification passe par
  `claude auth login`, le flux officiel ;
- environ **97 % de lecture de cache** mesurés lors des tests.

Ça respecte la lettre de la politique d'Anthropic : binaire non modifié, connexion par le flux
officiel, identifiants jamais manipulés, usage personnel. Le plugin indique que chaque tour est
décompté du **forfait « Agent SDK »** de l'abonnement.

Le même principe existe pour ChatGPT : le runtime **`codex app-server`** de Hermes confie des tours
entiers au binaire Codex officiel.

### Voie C — Déléguer des tâches entières au CLI ✅

Orion confie une tâche complète (« corrige ce bug ») au CLI `claude` ou `codex`, qui travaille avec
ses propres outils. C'est ce que j'avais proposé en D5. Toujours valable, et complémentaire de B.

### Côté ChatGPT

- Hermes appelle directement l'API Codex avec un jeton ChatGPT OAuth, **en s'identifiant
  honnêtement** (`originator: hermes-agent`). Un commentaire du code dit qu'OpenAI exige que les
  harnais tiers s'identifient. C'est toléré mais pas un programme officiel.
- Le programme officiel est « Sign in with ChatGPT » (sept. 2026), en aperçu limité à des
  partenaires, dont OpenClaw.

### Conclusion pour Orion

**L'abonnement Claude est utilisable**, via la voie B (Claude comme modèle) et la voie C (Claude
comme agent délégué). On n'utilisera **jamais** la voie A. Coût de la voie B : un lancement de
processus par appel, donc plus de latence ; à réserver au raisonnement profond, pas au chemin
réflexe de la voix. Risque : Anthropic peut changer sa politique ; il faut toujours un fournisseur
de repli (OpenRouter). → Mise à jour de D5 dans `DECISIONS.md`.

---

## 2. Hermes Agent — l'essentiel

**Ce qui est remarquable :**
- **« Le cache de prompt par conversation est sacré »** : le prompt système ne change jamais en
  cours de conversation. Tout ajout passe par un message utilisateur ou un résultat d'outil. La
  seule rupture autorisée est la compression.
- **« Le noyau est une taille de guêpe »** : toute nouvelle capacité arrive par un plugin, un skill
  ou un serveur MCP. Un nouvel outil du noyau est le dernier recours, car chaque schéma d'outil est
  envoyé à chaque appel.
- Discipline de code : CI qui impose ≤ 2 000 lignes par fichier et une complexité ≤ 20 par
  fonction. ~25 000 tests.
- **Stockage SQLite unique** (`state.db`, mode WAL) : sessions avec lignée, messages, recherche
  plein texte (FTS5, trigrammes, CJK), file d'entrée durable, registre des exécutions, **file de
  livraison** (outbox).
- **Boucle d'apprentissage** : tous les 10 tours, un **fork en arrière-plan** rejoue la conversation
  (sur le même préfixe de cache, donc pas cher) et se demande « faut-il retenir quelque chose ?
  créer un skill ? ». Un **curateur** fait vieillir les skills : actif → périmé (14 j) → archivé
  (30 j), jamais supprimé.
- **Trois niveaux multi-agent** :
  1. `delegate_task` : sous-agents en threads, 10 en parallèle, worktree git par enfant, rôles
     `leaf` (sans délégation ni mémoire ni envoi de messages) et `orchestrator`. Non durable.
  2. **Kanban** : tableau de tâches durable (SQLite) avec états, assignés, liens parent-enfant,
     **commentaires comme protocole entre agents**. Un répartiteur lance chaque worker comme un
     **processus séparé**. Reprise des tâches abandonnées après 4 h sans signe de vie. Contrats de
     fin (ex. une PR doit passer la CI).
  3. A2A et « Bot Mode » (groupes de bots qui se parlent).
- `execute_code` : un script Python appelle les outils de Hermes par RPC sur socket Unix ; seul
  `print()` revient au modèle. (Même idée que « code execution with MCP » d'Anthropic.)

**Ce qui pèche :**
- **14–15K tokens de schémas d'outils à chaque appel** (« 73 % de chaque appel est du coût fixe »).
- **Mémoire minuscule** : `MEMORY.md` 2 200 caractères, `USER.md` 1 375 caractères, figés au
  démarrage de la session. Comme une conversation de messagerie est **une session infinie**, la
  boucle d'apprentissage se déclenche rarement.
- Complexité et fragilité : centaines de fichiers d'état, bugs de verrous qui bloquent des
  conversations, fuites mémoire.
- **Pas de canal téléphonique entrant.** iMessage via BlueBubbles, un service tiers, ou un skill
  `imsg` — pas de vraie « main » sur le Mac.
- Sécurité honnête mais faible : *« la seule frontière de sécurité contre un LLM hostile est le
  système d'exploitation »* ; approbations et scanners sont des heuristiques.

---

## 3. OpenClaw — l'essentiel

C'est le système **le plus proche de l'architecture voulue pour Orion**.

**Architecture cerveau + mains, déjà éprouvée :**
- Une **passerelle** (Gateway) par hôte : un démon qui possède tous les canaux, les sessions, la
  planification et l'état.
- Protocole **WebSocket + JSON typé** : premier message obligatoirement `connect`, défi signé par
  l'identité de l'appareil, **appairage** des nouveaux appareils, clés d'idempotence pour toute
  action à effet de bord, événements sans rejeu.
- Les **nœuds** (Mac, iPhone, Android, Watch) se connectent au même serveur avec `role: "node"`,
  déclarent leurs capacités (caméra, écran, localisation, notifications, exécution, SMS…) et la
  passerelle les invoque. Un nœud peut aussi exposer des serveurs MCP et des modèles locaux.
- **Présence** : le Mac qui a reçu la dernière saisie est marqué « actif » → les alertes vont là
  où tu es physiquement.

**« Un seul cerveau » :** tous les messages privés du propriétaire, quel que soit le canal,
arrivent dans **une seule session principale**. Groupes, tâches planifiées et sous-agents ont
leurs sessions isolées et **remontent** vers elle. `identityLinks` fusionne les identités d'une
même personne sur plusieurs canaux.

**Mémoire, la plus aboutie des deux :**
- Markdown comme source de vérité, SQLite comme index dérivé.
- Fichiers : `SOUL.md` (personnalité), `USER.md` (modèle de l'utilisateur, entrées datées
  actives/remplacées), `MEMORY.md` (long terme), notes quotidiennes `memory/AAAA-MM-JJ.md`.
- Recherche hybride : BM25 + vecteurs + noms de fichiers → `pertinence × décroissance temporelle
  (demi-vie 30 j) × importance` → **MMR** pour la diversité.
- **Flush avant compaction** : un tour silencieux demande à l'agent de sauvegarder ce qui compte,
  en ajout seul.
- **Rêve** (« dreaming ») : consolidation nocturne en phases (léger, profond, REM) qui promeut des
  souvenirs dans `MEMORY.md`, avec un **filtre de contamination** (rien d'issu de contenu non fiable
  n'est promu) et un journal lisible `DREAMS.md`.
- **Intentions permanentes** : « quand X est mentionné, rappelle-moi Y ». Détection par mots-clés
  **sans appel modèle**, limite de déclenchements, expiration à 90 jours.

**Proactivité — et son recul :**
- Au début : un *heartbeat* toutes les 30 min qui « vérifie tout ». Résultat : du bruit et des
  tâches fantômes. Aujourd'hui, le prompt par défaut se termine par *« Si rien ne demande ton
  attention, réponds NO_REPLY »*, et le fichier `HEARTBEAT.md` a été **retiré**.
- À la place, des primitives séparées : automatisations (`at`, `every`, `cron`, **à la fin d'une
  commande**, **sur un flux de sortie**), **conditions en script** (un petit JS décide s'il faut
  réveiller le modèle → pas d'appel modèle quand il ne se passe rien), rythme proposé par l'agent
  lui-même (`next_check`), « ordres permanents » avec autorité et escalade.
- Règle de prompt **« Promised Work »** : promettre un travail futur crée une obligation ; l'agent
  doit organiser le suivi *avant* de finir son tour.

**iMessage :** BlueBubbles a été **abandonné**. Le chemin actuel est `imsg` (CLI de Peter
Steinberger) qui lit `~/Library/Messages/chat.db` et parle **JSON-RPC sur stdio**. Exige l'accès
complet au disque ; les fonctions riches (réactions, réponses en fil, frappe en cours, édition)
exigent **SIP désactivé**. Depuis un serveur distant, OpenClaw passe par… un **wrapper SSH** vers
le Mac, pas par son protocole de nœuds. C'est un point faible qu'Orion peut corriger.

**Voix — le bon modèle :**
- Téléphonie via Twilio, Telnyx ou Plivo ; appels entrants et sortants.
- Mode **temps réel** : un modèle vocal temps réel (OpenAI Realtime, Gemini Live) parle à
  l'appelant et **consulte le vrai agent via un outil** (`openclaw_agent_consult`), avec un raccourci
  rapide qui renvoie des extraits de mémoire avant la consultation complète. Politique d'outils
  dédiée au vocal (`safe-read-only` / `owner`).
- C'est exactement la séparation **réflexe / raisonnement** (hypothèse H4 de l'étape 1).

**Multi-agent :**
- `sessions_spawn` : sous-agents avec **annonce poussée** au parent (pas de boucle d'attente),
  profondeur ≤ 5, ≤ 5 enfants par agent, ≤ 8 en parallèle, arrêt en cascade, outils
  d'orchestration retirés aux feuilles, `context: "fork"`, worktree, modèles moins chers pour les
  enfants.
- **Swarm** : orchestration en **code JS** (« il n'y a pas de DSL de graphe, le programme est
  l'orchestration »), jusqu'à 32 en parallèle.
- Équipe prédéfinie : coordinateur (« chief of staff ») + chercheur + rédacteur + relecteur.
- **ACP** pour piloter des agents externes (Claude Code, Codex, Gemini CLI…) comme des sessions ;
  **A2A** pour les agents distants.

**Sécurité — l'histoire à ne pas répéter :**
- Des dizaines de milliers de passerelles exposées sur Internet ; une **faille RCE en un clic**
  (CVE-2026-25253 : l'interface faisait confiance à une URL passée en paramètre) ; **341 skills
  malveillants sur 2 857** dans le registre ClawHub (faux « prérequis » qui installaient un voleur
  de mots de passe macOS).
- Aujourd'hui encore : sandbox **désactivée par défaut**, session principale exécutée sur l'hôte,
  messages entre agents **autorisés par défaut**.

---

## 4. Les produits fermés : Dots, Muse, Grok Bot, Poke

### Ce qui converge dans les trois grands

| | OpenAI Dots | Meta Muse | Grok Bot |
|---|---|---|---|
| Ordinateur dédié | VM cloud + navigateur, « prendre la main » | « Secure VM » par utilisateur | VM Linux persistante |
| Proactivité | Recherche en arrière-plan **en lecture seule imposée par le code** + tâches planifiées | Suggestions non sollicitées, revient « quand quelque chose change » | Routines cron et événements, relances |
| Mémoire | Hérite de ChatGPT, **non consultable ni modifiable** (critiqué) | « Oublie X » possible | — |
| Multi-agent | « Équipes de Dots » annoncées, pas livrées | — | **Bots nommés par rôle, groupes de 2 à 6, messages qui réveillent le destinataire** |
| Canaux | App, Slack, Teams, SMS (bêta), appel entrant | App, **WhatsApp**, **adresse mail dédiée**, lunettes, porte-clés | Apps |
| Sécurité | Règles à 4 niveaux + vérificateur hors de portée + planchers fixes | **Sentinel** : agent gardien isolé par lequel passe tout le trafic sortant | Limites **en prose seulement** (critiqué) |

### Les idées à retenir

1. **Lecture seule pour la proactivité** (Dots). Quand l'agent travaille sans qu'on le lui demande,
   ses outils sont **en lecture seule par construction** ; il écrit des notes privées. Seuls les
   flux initiés par l'utilisateur ou autorisés par une règle ont des outils d'écriture. C'est la
   règle la plus simple pour rendre l'autonomie 24/7 sûre.
2. **Un gardien hors de portée de l'agent** (Sentinel de Muse, Auto-review de Dots). Un processus
   séparé, que l'agent ne peut pas modifier, valide toute sortie : **destinataire + contenu** pour
   un message.
3. **Taxonomie d'actions de Dots** : agir sans demander / agir si explicitement demandé / demander
   d'abord / passer la main. Plus des **planchers** non contournables (transfert d'argent,
   changement de mot de passe, suppression définitive, installation). *Une approbation ne crée
   aucune permission durable ; déléguer n'élargit jamais l'autorité.*
4. **Les identifiants ne sont jamais montrés au modèle.** Coffre-fort, injection côté navigateur,
   cartes virtuelles à usage unique (Link de Stripe chez Muse). Pour une connexion, un 2FA ou un
   CAPTCHA, l'humain prend la main (Grok).
5. **Journal des actions faites *et prévues*** (Muse), vue d'activité avec arrêt (Dots).
6. **Honnêteté** : le scandale Muse (des appels « IA » passés en réalité par des humains, sans le
   dire) et le blocage par Amazon (l'agent ne s'identifiait pas). L'agent doit **se présenter comme
   une IA** aux tiers.

### Poke — le plus proche de la vision « iMessage d'abord »

- **Aucune application** : iMessage, SMS, WhatsApp, Telegram. Inscription en une minute (numéro +
  un compte mail). Premier agent IA approuvé par Apple pour *Messages for Business* (juin 2026).
- Architecture rapportée : un **agent d'interaction** (une seule personnalité face à
  l'utilisateur) qui délègue à des **agents d'exécution** en arrière-plan.
- Retours : **~70 % des relances utiles, ~30 % de bruit** ; des mails importants manqués ; des
  **bugs de date et de fuseau horaire** (mauvais jour, rappel en retard d'une heure) qui détruisent
  la confiance.
- Personnalité volontairement insolente : virale, mais elle fait fuir une partie des utilisateurs.

### Les échecs instructifs

- **Daily Hub de Google** retiré, **Daily Brief** qui se désactive tout seul : *un flux proactif
  générique meurt*. Chaque relance doit être ancrée dans tes objectifs déclarés et dire **pourquoi**
  elle te parvient.
- **Alexa+** jugée « bavarde et qui interrompt », et en régression sur les tâches simples : *ne
  jamais dégrader les bases fiables*.
- **Dots** : la démo vocale a **échoué en direct** à la keynote. Il faut un repli (voix → texte
  sans perdre la tâche).
- **@grok** (incident de juillet 2025) : un changement de prompt en production est un déploiement
  critique, à évaluer et déployer progressivement.

---

## 5. Multi-agent : ce que dit l'état de l'art

### 5.1 Le débat est tranché, et la réponse est nuancée

- **Cognition (juin 2025), « Don't build multi-agents »** : les actions portent des **décisions
  implicites**, et des décisions contradictoires donnent de mauvais résultats (l'exemple du
  Flappy Bird dont le fond et l'oiseau sont dessinés dans deux styles différents).
- **Anthropic (juin 2025)** : le multi-agent gagne nettement (+90 %) sur la **recherche** —
  travail large, parallélisable, en lecture — et perd sur le travail à contexte partagé comme le
  code. Coût ≈ **15×** les tokens d'une conversation.
- **Cognition (avril 2026), mise à jour** : *« un seul agent écrit à la fois ; les autres apportent
  de l'intelligence, pas des actions »*. Trois motifs qui marchent :
  1. **un relecteur à contexte neuf** qui ne voit que le diff (≈ 2 bugs par PR, 58 % graves) ;
  2. **l'« ami intelligent »** : un modèle bon marché appelle un modèle fort en lui passant un
     **fork du contexte complet** ; les paires de fournisseurs différents (Claude + GPT) aident sur
     les cas durs ;
  3. **un manager** qui répartit et suit des agents enfants.
- **Google/MIT (déc. 2025)**, l'étude la plus rigoureuse :
  - les erreurs sont amplifiées **17,2×** quand les agents sont indépendants, **4,4×** avec un
    orchestrateur central ;
  - **toutes** les variantes multi-agent **dégradent de 39 à 70 %** les tâches de raisonnement
    séquentiel ;
  - un modèle prédictif choisit la bonne architecture dans 87 % des cas.

**Règle qui en sort :** paralléliser la **lecture** (recherche, relecture, exploration
d'hypothèses) ; paralléliser l'**écriture** seulement avec une **propriété exclusive** des
ressources et un **oracle** (tests) qui rend les échecs indépendants ; garder le **raisonnement
séquentiel** dans un seul agent.

### 5.2 Pourquoi les systèmes multi-agents échouent (MAST, NeurIPS 2025)

1 642 traces annotées sur 7 frameworks, 14 modes d'échec :

| Catégorie | Part | Modes principaux |
|---|---|---|
| Conception du système | ≈ 44 % | répétition d'étapes 15,7 % · ignorer la condition d'arrêt 12,4 % · désobéir à la consigne 11,8 % |
| Désalignement entre agents | ≈ 32 % | écart raisonnement/action 13,2 % · dérive de la tâche 7,4 % · ne pas demander de clarification 6,8 % |
| Vérification | ≈ 24 % | vérification incorrecte 9,1 % · absente 8,2 % · arrêt prématuré 6,2 % |

**La plupart des échecs viennent de la conception, pas du modèle.** Ajouter une étape de
vérification de l'objectif : **+15,6 %**. → La terminaison, la détection de boucles et la
vérification sont des **responsabilités du noyau**.

**Who&When (ICML 2025)** : retrouver après coup quel agent a causé un échec marche 53,5 % du temps,
mais trouver **l'étape décisive** seulement **14,2 %**. → Il faut enregistrer la **provenance au
moment de l'écriture**, pas compter sur une analyse a posteriori.

**Débat entre agents** : à budget égal, le débat **ne bat pas** la simple auto-cohérence (plusieurs
tirages + vote). Pire, les agents **changent une bonne réponse pour une mauvaise** après avoir vu
celle des pairs (conformisme, flagornerie). Ce qui aide : des **modèles différents** et des agents
qui s'engagent **avant** de voir les autres.

**Project Vend** (Anthropic) : un agent « PDG » du même modèle que l'agent supervisé n'apporte
presque rien — *les superviseurs du même modèle partagent les mêmes angles morts*. Un rôle
**nettement séparé** aide. L'outillage compte plus que l'ajout de managers.

### 5.3 Les primitives qui marchent en production

| Primitive | Exemple | Leçon |
|---|---|---|
| Liste de tâches partagée + verrous de réclamation | Équipes Claude Code, Kanban Hermes | Les agents **oublient de marquer « fini »** → bloque les dépendances |
| Boîtes aux lettres (acteurs) | Équipes Claude Code, AutoGen 0.4 | Livraison **poussée**, messages typés, jamais d'autorité |
| Stigmergie (traces dans l'environnement) | Compilateur C : fichiers de verrou + git comme mutex | Pas d'orchestrateur, passe à l'échelle sans messages |
| Hiérarchie planificateur → workers → juges | Cursor (centaines d'agents) | Les pairs « à plat » avec verrous ont échoué : **les LLM gardent les verrous trop longtemps** |
| Registres de tâche et de progrès | Magentic-One | À chaque étape : demande satisfaite ? on boucle ? on progresse ? qui parle ? → compteur de blocage → replanification |
| Artefacts structurés plutôt que bavardage | MetaGPT (PRD, specs), Anthropic (fichiers + références) | Évite le « téléphone arabe » |
| Transfert verbatim | LangChain `forward_message` | +50 % pour le superviseur qui paraphrasait |
| Orchestration en code | Workflows Claude Code, Swarm OpenClaw | Le plan est lisible, diffable, rejouable |
| Orchestrateur appris | Kimi PARL, AgentFlow, AsyncThink | On entraîne **le coordinateur**, pas les workers. Piège : « effondrement en série » |

---

## 6. Synthèse : convergences, angles morts, opportunités pour Orion

### 6.1 Ce sur quoi tout le monde converge (à reprendre)

1. **Une personnalité devant, beaucoup d'ouvriers derrière** (Poke, Dots, OpenClaw, Grok « chief
   of staff »). L'utilisateur parle à une seule identité ; les ouvriers rendent compte à elle, et
   c'est elle qui décide si et quand te parler.
2. **Un seul cerveau** pour toutes tes conversations privées, quel que soit le canal (OpenClaw).
3. **Cache de prompt sacré** (Hermes, OpenClaw, Claude Code).
4. **SQLite comme colonne vertébrale** : sessions, recherche plein texte, files durables, outbox
   (Hermes, OpenClaw).
5. **Markdown lisible comme source de vérité de la mémoire**, index dérivés (OpenClaw).
6. **Proactivité découpée en primitives** + silence par défaut (`NO_REPLY`, `[SILENT]`).
7. **Voix temps réel qui consulte l'agent complet** (OpenClaw).
8. **CLI officiels comme fournisseurs** pour les abonnements (Hermes DirectSDK, Codex app-server).
9. **Sous-agents à annonce poussée**, profondeur et parallélisme bornés, feuilles sans outils
   d'orchestration.

### 6.2 Les angles morts (là où Orion peut vraiment innover)

| Angle mort constaté | Chez qui | Opportunité Orion |
|---|---|---|
| Proactivité soit bruyante (heartbeat), soit éteinte (`NO_REPLY` par défaut) | OpenClaw, Poke (30 % de bruit), Alexa+ | **Gestion de l'attention** comme sous-système : budget d'interruption, calibration par retour, ancrage dans tes objectifs, explication du « pourquoi » |
| Mémoire minuscule ou figée, non modifiable, sans provenance | Hermes (1,3K tokens), Dots (non modifiable) | **Mémoire vivante** : typée, sourcée, révisable, « déconnecter Gmail » = « oublier ce qui vient de Gmail », consolidation et contradictions |
| Une session de messagerie infinie comme frontière de mémoire | Hermes | **Contexte recompilé à chaque appel** depuis l'état (H2) : la notion de « session infinie » disparaît |
| iMessage via SSH ou service tiers ; fonctions riches = SIP désactivé | OpenClaw, Hermes | **Main Mac de premier rang** qui héberge iMessage dans le protocole de nœuds |
| Pas de téléphonie entrante | Hermes, Dots (ne peut pas t'appeler) | **Voix bidirectionnelle** dès le départ, avec repli texte |
| Sécurité faite de prose ou d'heuristiques ; ouvert par défaut | Grok Bot, OpenClaw, Hermes | **Sécurisé par défaut** : gardien séparé, politiques appliquées par le code, planchers, simulation, messages entre agents refusés par défaut |
| Multi-agent sans mémoire de décisions partagée | Tous | **Journal de décisions partagé** + détecteur de conflits |
| Échecs multi-agents détectés après coup | Tous | **Provenance à l'écriture** + rejeu contrefactuel |
| Superviseurs du même modèle que les ouvriers | Tous | **Hétérogénéité imposée** : le vérificateur utilise un autre modèle |
| Proactivité qui écrit autant qu'elle lit | Grok Bot, OpenClaw | **Lecture seule par construction** hors demande explicite (Dots) |
| Erreurs de date et de fuseau | Poke | Outils **déterministes** pour toute arithmétique temporelle, jamais le LLM |

### 6.3 Principes multi-agent pour le noyau d'Orion

Retenus de l'état de l'art (détail dans le rapport de recherche) :

1. **Un seul écrivain par ressource** à tout instant (fichier, branche, canal vers l'utilisateur).
   Les autres lisent, cherchent, relisent, conseillent.
2. **La topologie est un choix typé, déduit de la forme de la tâche** : lecture parallèle →
   éventail ; écriture parallèle → propriété partitionnée + oracle ; raisonnement séquentiel → un
   seul agent. Le choix est journalisé.
3. **Hiérarchie par défaut** (planificateur → ouvriers → juge), essaims seulement pour le travail
   « embarrassingly parallel ».
4. **Quatre sémantiques de délégation distinctes dans le noyau** :
   - `call` : l'agent comme outil, renvoie un résultat ;
   - `fork` : copie le contexte et le préfixe de cache ;
   - `handoff` : transfère le contrôle et la propriété ;
   - `spawn` : pair durable avec boîte aux lettres.
5. **Des références, pas des résumés** : les enfants écrivent des artefacts et renvoient
   `{artefact, résumé, confiance, questions ouvertes}`. Le parent transmet **verbatim**.
6. **Journal de décisions partagé**, séparé des brouillons : interfaces, style, choix techniques,
   hypothèses → reçus par tous les agents concernés.
7. **La consigne de délégation est un contrat** : objectif, périmètre et propriété, format de
   sortie, condition d'arrêt, budget, outils autorisés, ce qu'il ne faut **pas** faire.
8. **La terminaison est l'affaire du noyau** : détection de répétitions, registre de progrès,
   compteur de blocage.
9. **La vérification est une étape séparée** : contexte neuf, autre prompt, **autre modèle**,
   oracles exécutables de préférence.
10. **Concevoir pour des échecs indépendants** (l'astuce de l'oracle GCC).
11. **Baux à durée limitée + compare-and-swap**, jamais de verrou nu.
12. **Un espace de travail isolé par écrivain**, fusion = étape explicite du noyau, testée.
13. **Concurrence structurée** : arbre d'agents ; annuler un parent annule son sous-arbre ;
    détacher est explicite.
14. **Budgets hiérarchiques appliqués par le code** (tokens, argent, temps, appels).
15. **Optimiser le chemin critique, pas le nombre d'agents.**
16. **Messages typés, poussés, à débit limité** (`question`, `résultat`, `statut`, `arrêt`…).
17. **Un message d'agent n'est jamais une autorité.**
18. **Consensus préservant l'indépendance** : chacun s'engage avant de voir les autres.
19. **Points de reprise durables** pour chaque agent et chaque orchestration.
20. **Provenance enregistrée à l'écriture.**
21. **Hygiène de contexte** : sorties courtes, erreurs sur une ligne, fichiers de progrès,
    redémarrages à neuf périodiques contre la dérive.
22. **Orchestration programmable** pour les gros travaux.

### 6.4 Idées nouvelles à explorer (aucun système ne les fait bien)

1. **Détecteur de conflits de décisions** : les agents émettent des décisions typées ; un démon
   les compare entre agents concurrents et lève le conflit **avant** la fusion.
2. **Appel d'offres sensible au contexte** : une tâche va à l'agent dont le cache contient déjà
   les bons fichiers, ou au modèle le moins cher capable de la faire.
3. **Ordonnanceur de chemin critique avec exécution spéculative** : démarrer sur un petit modèle
   les tâches probables, valider ou jeter quand les prédécesseurs finissent.
4. **Rejeu contrefactuel** : grâce aux points de reprise, rejouer un échec en changeant la sortie
   d'un seul agent pour trouver l'étape décisive par intervention.
5. **L'hétérogénéité comme ressource du noyau** : vérificateurs et superviseurs reçoivent
   automatiquement un autre modèle que les générateurs.
6. **Arbres de supervision à la Erlang** : redémarrer à neuf (avec fichier de progrès) en cas de
   blocage, escalader après N redémarrages.
7. **Topologie qui s'auto-ajuste** sur les traces d'Orion lui-même, pour **toi** (bandit ou
   recherche à la AFlow).
8. **« Guilde de recherche » proactive** : un pool permanent d'agents en lecture seule qui
   prépare tes prochains rendez-vous et mails sous budget quotidien ; les résultats ne te
   parviennent que par l'agent qui te fait face.
9. **Le désaccord comme signal** : quand des agents indépendants divergent, le noyau augmente le
   calcul (plus d'échantillons, modèle plus fort, oracle) ou te demande ; quand ils sont d'accord,
   il économise.

---

## 7. Ce que ça change pour l'architecture (pistes pour l'étape 4)

Une première silhouette se dessine, à discuter :

```
                    ┌────────────────────── Toi ──────────────────────┐
                    │ iMessage · appel téléphonique · terminal · …    │
                    └───────────────────────┬─────────────────────────┘
          ┌─────────────────────────────────▼──────────────────────────────────┐
          │  CERVEAU (serveur)                                                  │
          │  ┌───────────────┐   ┌──────────────────┐   ┌───────────────────┐  │
          │  │ Agent de      │◀─▶│ Gestion de       │◀─▶│ Flux d'événements │  │
          │  │ présence      │   │ l'attention      │   │ (journal durable) │  │
          │  │ (une identité)│   └──────────────────┘   └─────────▲─────────┘  │
          │  └──────┬────────┘                                    │            │
          │         │ délègue (call/fork/handoff/spawn)           │            │
          │  ┌──────▼──────────────────────────────────┐   ┌──────┴─────────┐  │
          │  │ Noyau multi-agent : tâches, baux, budgets, │ │ Mémoire vivante │  │
          │  │ journal de décisions, vérificateurs     │◀─▶│ + consolidation │  │
          │  └──────┬──────────────────────────────────┘   └────────────────┘  │
          │  ┌──────▼──────────────┐   ┌──────────────────────────────────┐    │
          │  │ Gardien (processus  │   │ Fournisseurs : OpenRouter · API · │    │
          │  │ séparé, politiques) │   │ CLI claude/codex (abonnements)   │    │
          │  └──────┬──────────────┘   └──────────────────────────────────┘    │
          └─────────┼──────────────────────────────────────────────────────────┘
                    │ protocole de nœuds (WS signé, appairage, capacités)
       ┌────────────┼─────────────────┬──────────────────────┐
  ┌────▼─────┐ ┌────▼──────┐   ┌──────▼──────┐        ┌──────▼──────┐
  │ Mac      │ │ iPhone    │   │ Téléphonie  │        │ Sandboxes   │
  │ iMessage │ │ notifs,   │   │ (voix temps │        │ d'exécution │
  │ fichiers │ │ position  │   │ réel)       │        │ jetables    │
  └──────────┘ └───────────┘   └─────────────┘        └─────────────┘
```

---

## Sources principales

**Open source (code lu)**
- Hermes Agent : https://github.com/NousResearch/hermes-agent · docs https://hermes-agent.nousresearch.com/docs/
- Plugin DirectSDK : https://github.com/NousResearch/hermes-official-plugins/tree/main/claude-subscription-directsdk
- Issue Hermes sur l'usage des jetons Claude : https://github.com/NousResearch/hermes-agent/issues/133406
- OpenClaw : https://github.com/openclaw/openclaw · docs https://docs.openclaw.ai
- imsg : https://github.com/steipete/imsg

**Produits**
- Dots : https://techcrunch.com/2026/09/29/openai-launches-dots-its-bubbly-agentic-avatar/ · https://www.nbcnews.com/tech/tech-news/openai-launches-dots-ai-agents-safety-questions-rcna600338
- Muse : https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/ · https://404media.co/meta-tests-muse-ai-agent-calls-that-are-actually-made-by-humans-in-a-call-center
- Grok Bot : https://venturebeat.com/orchestration/spacexais-grok-bot-turns-agents-into-persistent-digital-coworkers-that-can-operate-your-apps-for-120-per-month
- Poke : https://blog.saner.ai/poke-reviews/
- Gemini Spark : https://blog.google/innovation-and-ai/products/gemini-app/next-evolution-gemini-app/

**Sécurité OpenClaw**
- https://thehackernews.com/2026/02/openclaw-bug-enables-one-click-remote.html
- https://thehackernews.com/2026/02/researchers-find-341-malicious-clawhub.html

**Multi-agent**
- https://cognition.ai/blog/dont-build-multi-agents · https://cognition.com/blog/multi-agents-working
- https://www.anthropic.com/engineering/multi-agent-research-system · https://www.anthropic.com/engineering/building-c-compiler
- https://code.claude.com/docs/en/agent-teams
- https://cursor.com/blog/scaling-agents
- https://www.langchain.com/blog/benchmarking-multi-agent-architectures
- MAST : https://arxiv.org/abs/2503.13657 · Who&When : https://arxiv.org/abs/2505.00212
- Google/MIT : https://arxiv.org/abs/2512.08296 · Débat : https://arxiv.org/abs/2502.08788
- AgentFlow : https://arxiv.org/abs/2510.05592 · AsyncThink : https://arxiv.org/abs/2510.26658
- Magentic-One : https://arxiv.org/abs/2411.04468
