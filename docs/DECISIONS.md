# Décisions d'architecture

Journal des décisions prises pour Orion. Chaque entrée indique le contexte, la décision et ses
conséquences. On ne réécrit pas une décision : on en ajoute une nouvelle qui la remplace.

---

## D1 — Topologie : cerveau central + mains distribuées

*19/10/2026 · acceptée*

**Décision.** Le « cerveau » d'Orion (boucle, mémoire, journal d'événements, planification) tourne
sur un serveur toujours allumé. Des « mains » tournent sur les appareils de l'utilisateur (Mac,
téléphone…) et exposent des capacités au cerveau : envoyer un iMessage, lire un fichier local,
exécuter une commande, etc.

**Conséquences.**
- Il faut un protocole cerveau ↔ mains authentifié, qui tolère qu'une main soit hors ligne.
- Un Mac est obligatoire comme main pour iMessage : Apple n'a pas d'API publique, il faut un relais
  local (ex. BlueBubbles, ou un pont maison sur la base de données Messages).
- Cela rejoint le modèle « Managed Agents » d'Anthropic : le cerveau est découplé des mains, et une
  main n'est qu'un outil.

## D2 — Canaux : le plus possible, iMessage et appels en priorité

*19/10/2026 · acceptée*

**Décision.** Les canaux sont des modules interchangeables. Les deux premiers visés sont **iMessage**
et la **voix par appel téléphonique**, parce qu'ils sont les plus proches d'une relation humaine.
Viendront ensuite le terminal (pour le développement), Telegram, le mail, etc.

**Conséquences.**
- La voix impose une contrainte de latence (< 1 s pour répondre). Il faudra un chemin « réflexe »
  rapide, distinct du raisonnement profond (cf. hypothèse H4 de l'étape 1).
- La téléphonie passe par un fournisseur (numéro virtuel + flux audio temps réel) ; choix à faire à
  l'étape d'implémentation.

## D3 — Périmètre : toute la vie de l'utilisateur

*19/10/2026 · acceptée*

**Décision.** Orion est un second cerveau généraliste : projets, personnes, agenda, mails, finances,
santé, notes. La mémoire et les permissions sont organisées **par domaine de vie**, pas par
répertoire.

**Conséquences.**
- Données très sensibles : chiffrement au repos, identifiants hors de portée du modèle,
  confinement strict des exécutions, journal d'audit.
- Les actions à effet externe (envoyer, payer, supprimer, publier) exigent une politique dédiée :
  simulation, brouillon, délai d'annulation.

## D4 — Langage : Python

*19/10/2026 · acceptée (révisable composant par composant)*

**Décision.** Python 3.12+ en `asyncio` pour le noyau. Préférence de l'utilisateur, écosystème
IA/données le plus riche, SDK MCP officiel disponible.

**Garde-fous** (leçons de l'ancien Orion) :
- `asyncio` de bout en bout plutôt que des threads partageant des verrous.
- Paquets découpés par responsabilité, avec des interfaces (`Protocol`) explicites.
- Typage strict (`mypy`/`pyright`), logs structurés, aucun `except: pass`.
- Un composant peut être réécrit dans un autre langage s'il le justifie (ex. relais iMessage en
  Swift sur le Mac, pipeline audio temps réel).

## D5 — Modèles : multi-fournisseurs

*19/10/2026 · acceptée, avec contraintes de conditions d'utilisation*

**Décision.** Le noyau parle à une interface `Provider` neutre. Fournisseurs visés : OpenRouter
(API), Claude et ChatGPT via les abonnements de l'utilisateur.

**Contraintes vérifiées le 19/10/2026 :**
- **Abonnement Claude (Pro/Max).** La page *Legal and compliance* de Claude Code interdit de
  faire passer des requêtes d'une application tierce par les identifiants d'un abonnement, et de
  collecter ou relayer ses jetons. Elle autorise en revanche d'utiliser le **binaire Claude Code
  officiel, non modifié**, connecté à son propre abonnement.
  ⇒ Orion ne pourra pas utiliser l'abonnement Claude comme « cerveau » direct. Il pourra
  **déléguer des tâches** au binaire `claude` officiel (ex. un worker pour le code), qui consomme
  l'abonnement normalement. Pour appeler Claude directement : clé API ou OpenRouter.
- **Abonnement ChatGPT (Plus/Pro).** OpenAI a lancé en septembre 2026 un « Sign in with ChatGPT »
  qui permet à des applications tierces d'utiliser le forfait de l'utilisateur. Il est pour
  l'instant en **aperçu limité à des partenaires**. Les contournements par jetons Codex ne sont pas
  supportés. ⇒ Même approche en attendant : délégation au CLI `codex` officiel, et intégration
  directe si le programme s'ouvre.

**Conséquences.**
- Deux familles de fournisseurs : *API* (appel direct, contrôle total du contexte) et *agent
  délégué* (un CLI officiel à qui on confie une tâche entière). Le noyau doit gérer les deux.
- Le routage choisit le modèle par tâche : coût, latence, capacité.

## D6 — Noyau propre, pas de framework

*19/10/2026 · acceptée*

**Décision.** Orion a son propre noyau. On ne construit ni sur l'Agent SDK de Claude ni sur un
framework d'agents. Les ruptures visées (flux d'événements continu, contexte recompilé, mémoire
vivante, gestion de l'attention) touchent justement la boucle et le contexte, que ces outils
imposent.

**Conséquences.**
- On réimplémente les éléments solides identifiés à l'étape 1 (contrat d'outil fail-closed, budgets
  de sortie, conception guidée par le cache, hooks, sandbox) au lieu d'en hériter.
- Compatibilité MCP obligatoire pour profiter de l'écosystème d'outils.

## D7 — Plusieurs instances d'Orion qui travaillent ensemble

*19/10/2026 · acceptée · précisée le même jour*

**Précision de l'utilisateur.** « Multi-agent » veut dire : **plusieurs instances complètes
d'Orion** (chacune avec son identité, sa mémoire, ses outils, son environnement, potentiellement
sur des machines différentes) qui forment des équipes. Ce n'est pas qu'Orion soit un harnais
orchestrant des sous-agents internes.

**Décision.** La collaboration entre instances est une capacité de premier rang du noyau, pensée
dès le départ.

**Conséquences.**
- Chaque instance a une **identité propre** et authentifiable ; la délégation entre instances
  transmet une **autorité restreinte**, jamais plus large que celle du délégant.
- Le protocole entre instances (tâches, messages, artefacts, baux, budgets, annulation) est une
  interface du noyau, pas une surcouche. Le standard A2A est le candidat naturel pour l'interop.
- Il faut décider ce qui se partage entre instances (mémoire, décisions, artefacts) et ce qui reste
  privé.
- Les sous-agents internes éphémères restent utiles (ex. explorer sans polluer le contexte), mais
  ce n'est pas l'axe principal.
- Les principes de l'étape 2 (§5–6 de `02-paysage.md`) s'appliquent toujours : un seul écrivain par
  ressource, vérification séparée, provenance, budgets, coût élevé du parallélisme.
- Un agent délégué externe (CLI `claude`, `codex`, cf. D5) peut être membre d'une équipe.

## D5-bis — Abonnement Claude : à réexaminer

*19/10/2026 · tranchée — complète D5*

L'utilisateur signale que Hermes Agent permettrait d'utiliser l'abonnement Claude. Vérifié dans le
code de Hermes (étape 2, `docs/research/02-paysage.md` §1) : il existe trois voies.

**Décision.**
- **Voie B adoptée : « CLI comme modèle ».** Pour un appel modèle, Orion lance le binaire `claude`
  officiel non modifié (`stream-json`, un seul tour), désactive ses outils natifs, lui annonce les
  outils d'Orion via MCP et les exécute lui-même. L'authentification passe uniquement par
  `claude auth login`. Modèle : le plugin `claude-subscription-directsdk` de Hermes. Même principe
  pour ChatGPT via `codex app-server`.
- **Voie C adoptée : « CLI comme agent délégué »** (D5 inchangée).
- **Voie A refusée : réutiliser les jetons OAuth en se faisant passer pour Claude Code.** C'est de
  l'usurpation d'identité contraire à la politique d'Anthropic ; en pratique elle ne consomme même
  pas le forfait de base.

**Conséquences.**
- L'abonnement Claude peut servir de « cerveau » pour le raisonnement profond. Pas pour le chemin
  réflexe de la voix : un processus lancé par appel ajoute de la latence.
- Un fournisseur de repli (OpenRouter) reste obligatoire : la politique d'Anthropic peut changer.

## D8 — Collaboration entre les Orion de personnes différentes

*19/10/2026 · acceptée*

**Décision.** Les instances d'Orion de **personnes différentes** doivent pouvoir collaborer :
s'organiser, échanger, négocier, travailler ensemble pour le compte de leurs humains respectifs.
C'est un axe stratégique, pas une option.

**Conséquences.**
- Chaque instance a une identité vérifiable par des tiers, et chaque relation (instance ↔ instance,
  humain ↔ humain) porte ses propres permissions.
- **Frontière de confidentialité** explicite : ce que mon Orion peut révéler de moi à ton Orion est
  une politique, pas une improvisation du modèle.
- Tout ce qui vient d'une instance étrangère est **non fiable par défaut** (injection de prompt,
  manipulation, ingénierie sociale entre agents) et ne porte jamais l'autorité de son humain.
- Il faut un protocole ouvert et interopérable (A2A comme candidat), une traçabilité des échanges et
  une notion de confiance/réputation entre instances.
- Un engagement pris au nom de l'utilisateur (rendez-vous, accord, dépense) suit les mêmes planchers
  d'approbation que toute action à effet externe (D3).

## D9 — iMessage sans Mac (révise P1 de l'étape 3)

*19/10/2026 · acceptée*

**Contexte.** L'utilisateur n'a pas de Mac. Les options « `imsg` sur ton Mac » (lire ton historique,
écrire en ton nom) sont donc indisponibles.

**Décision.** Phase 1 : Orion a **sa propre ligne iMessage gérée par un fournisseur** (Inkbox pour
démarrer gratuitement avec son SDK Python ; Sendblue ou Linq si besoin de groupes ou de FaceTime).
Tu écris en premier une fois, ensuite Orion peut être proactif. Telegram ou SMS en secours si la
ligne est perdue.

**Conséquences.**
- Orion ne lit pas ton historique iMessage existant et n'écrit pas « en ton nom » ; il écrit aux
  autres en tant qu'assistant, depuis son numéro, en se présentant.
- Pistes à évaluer plus tard, sans Mac : (a) analyser périodiquement une **sauvegarde locale de
  l'iPhone** (elle contient la base des messages ; lisible sous Windows/Linux, pas en temps réel) ;
  (b) automatisations **Raccourcis iOS** déclenchées à la réception d'un message, qui transmettent
  à Orion (capacités à vérifier) ; (c) un Mac mini dédié si le besoin devient fort.

## D10 — Voix (P2)

*19/10/2026 · acceptée*

Twilio + passerelle **Pipecat** (Python) + modèle vocal temps réel interchangeable (GPT-Live 1 en
premier, Gemini 3.8 Live en comparaison, chaîne STT→LLM→TTS en repli). Numéro américain pour
développer, puis numéro géographique français (pas de 06/07). Le modèle vocal est une façade : il
consulte Orion par des outils à deux vitesses. Niveaux d'identité L0–L3 appliqués par le code ;
aucune action irréversible à la voix seule.

## D11 — Mémoire (P3)

*19/10/2026 · acceptée*

Journal d'épisodes **immuable** comme source de vérité ; faits **bi-temporels** (validité dans le
monde / connaissance par Orion) avec **provenance** et niveau de confiance ; Markdown lisible pour
le profil, les personnes et les projets ; SQLite (plein texte + vecteurs + graphe) comme index
reconstructible ; **registre des engagements** (je dois / on me doit) de premier rang ; oubli par
source vérifiable ; évaluation maison dès le départ.

## D12 — Proactivité (P4)

*19/10/2026 · acceptée*

Moteur d'initiative dédié, distinct des conversations : règles déterministes → tri par petit
modèle → arbitre d'utilité (coût du retard vs coût d'interruption, budget, réglages). Silence par
défaut, 3 résumés par jour, échelle jusqu'à l'appel, « pourquoi maintenant » sur chaque message,
travail en arrière-plan **en lecture seule par construction**, autonomie par type d'action promue
uniquement avec l'accord de l'utilisateur.

## D13 — Collaboration entre instances (P5)

*19/10/2026 · acceptée*

A2A v1.0 comme protocole ; chaque instance est un principal cryptographique ; délégation par jetons
qui ne font que restreindre (Biscuit). Entre personnes : présentation humaine ou par un tiers de
confiance, relations comme objets de premier rang, négociateur isolé avec enveloppe de divulgation,
secrets sous forme de prédicats, propositions typées, engagements co-signés, reçus mutuels. Premier
cas d'usage : organiser un rendez-vous entre deux personnes.

## D14 — Sécurité séparée de l'agent (P6)

*19/10/2026 · acceptée*

Un gardien distinct de l'agent (pare-feu sur les contenus entrants, moniteur d'actions, moteur de
politiques) que l'agent ne peut ni modifier ni convaincre. Jamais données privées + contenu non
fiable + canal de sortie dans un même processus.

## D15 — Personnalité d'Orion

*19/10/2026 · acceptée*

Tutoiement, humour, concision, précision, une pointe de sarcasme, et une expression **humaine** :
phrases naturelles, pas de listes à puces dans un SMS, pas de formules d'assistant (« Bien sûr !
Voici… »). Cette personnalité est écrite dans la constitution d'Orion (jalon M1) et évaluée comme
le reste : un changement de ton est un déploiement (leçon de l'incident @grok).

## D16 — Langue du code et conventions

*19/10/2026 · acceptée*

Code, identifiants et commentaires en anglais ; documentation et décisions en français. Python 3.12+,
`uv`, Pydantic v2, `pyright` strict, `ruff`, `pytest`. Infra existante de l'utilisateur : un serveur
(hébergement du cerveau) et un compte OpenRouter (fournisseur du jalon M1). Pas de budget LLM
plafonné, mais budgets par tâche appliqués par le code (garde-fou contre les boucles).

## D17 — La mémoire voyage dans les messages, jamais dans le prompt système

*19/10/2026 · acceptée*

Pour préserver le cache de prompt, la mémoire n'est jamais injectée dans le message système. Elle
est jointe au message de l'utilisateur au moment où il arrive, puis rejouée telle quelle :
- `<memoire>` (profil, préférences, engagements ouverts) au premier message d'une fenêtre de
  conversation (nouveau fil ou après un rebase) ;
- `<souvenirs>` (faits pertinents trouvés par recherche plein texte) sur chaque message, sans
  répéter ce qui est déjà visible dans la fenêtre.

Les écritures en mémoire sont des outils à effet interne qui refusent les contextes non fiables :
un contenu lu sur le web ou venant d'un tiers ne peut pas devenir un souvenir sans le
propriétaire. L'extraction en arrière-plan produit des souvenirs de confiance `SYSTEM` au plus.
La résolution d'entités (personnes) reste minimale en M2 (noms libres) et sera traitée en M6.
