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

## D18 — iMessage par Inkbox, en polling par défaut

*19/10/2026 · acceptée*

Le canal iMessage passe par une ligne gérée Inkbox, via un petit client asynchrone écrit pour Orion
(le SDK officiel est synchrone). Deux modes de réception, qui passent par le même point d'entrée :
- **polling** (défaut) : aucun port ouvert ni URL publique, quelques secondes de latence ;
- **webhook** : instantané, fermé par défaut (sans clé de signature, tout est refusé ; signature
  HMAC et horodatage vérifiés ; accusé de réception seulement après journalisation).

Seul le numéro du propriétaire est écouté. Les autres messages sont journalisés comme contenus
tiers et restent sans réponse. Chaque message entrant est journalisé avant tout traitement, avec
son identifiant : après un crash, ce qui n'a pas reçu de réponse est repris, rien n'est traité
deux fois. Les rafales de messages courts sont regroupées en un seul tour (3 s). Tous les canaux
du propriétaire partagent **un seul fil** (`owner`), et donc un seul préfixe en cache.

## D19 — Outbox idempotente et approbations par message

*19/10/2026 · acceptée*

Tout envoi est journalisé (`outbox.queued`) avant de partir. Son identifiant sert de clé
d'idempotence chez le fournisseur : après un crash, l'envoi est rejoué avec la même clé, sans
risque de doublon. Un refus net (4xx) n'est jamais rejoué.

Les approbations du gardien passent par iMessage : l'action exacte, puis un tapback (👍/👎) ou un
mot (oui/non). Sans réponse avant le délai (15 min), c'est non. Une approbation vaut pour cette
action seulement.

Le gardien reste pour l'instant dans le processus d'Orion, derrière ses interfaces (`Approver`,
`Outbox`, `Transport`). Aucun outil d'Orion n'a encore d'effet externe autre que te répondre. La
séparation en processus (D14) se fera avec le premier outil qui agit sur le monde (mail, agenda),
sans changer ces interfaces.

## D20 — Initiative v0 : des règles décident, le modèle rédige

*19/10/2026 · acceptée*

Le moteur d'initiative tourne toutes les 30 s. Il examine trois sources :
- les rappels demandés (`remind_me`) ;
- trois points par jour (8h30, 13h, 19h), construits à partir du registre des engagements (deux depuis D144) ;
- une alerte une heure avant un engagement à heure précise.

La décision de parler est **déterministe** : heures calmes (22h30 à 8h), budget quotidien
d'attention (6 messages non demandés), et un registre de ce qui a déjà été dit, pour ne jamais se
répéter, y compris après un redémarrage. Un point sans rien de nouveau n'est pas envoyé et ne
coûte aucun appel au modèle. Les rappels que tu as demandés passent outre les heures calmes et le
budget.

Le modèle rédige le message, avec son « pourquoi maintenant », dans le fil du propriétaire. Ta
réponse arrive donc avec le contexte. Le modèle peut répondre `SILENCE` pour un point qui ne
mérite pas de te déranger ; pour un rappel demandé, un texte de secours part quand même. Chaque
décision, y compris celle de se taire, est journalisée avec sa raison.

`remind_me` est la seule façon pour Orion de revenir vers toi plus tard. La constitution lui
interdit de promettre un rappel sans l'avoir programmé (anomalie observée en M2).

## D21 — Orion apprend son propriétaire : le manuel

*19/10/2026 · acceptée*

Orion s'adapte à toi tout seul, et tout ce qu'il apprend reste lisible, corrigeable et oubliable.
Ce qu'il apprend forme ton **manuel** : des règles (« Pas d'emoji », « Le matin, deux phrases
max ») et des réglages que le code applique (heures des points, heures calmes, budget
d'attention). Il apprend par trois voies :

1. **Ce que tu lui dis** : une remarque sur sa manière devient une règle tout de suite (outil
   `adapt`). Changer le moment où il parle passe par `initiative_settings`.
2. **Ce qu'il observe** : une réflexion en lot, sur un modèle économique, relit tes derniers
   messages à côté de ses réponses, tes tapbacks sur ses messages, et la façon dont tu as réagi
   quand il a écrit le premier. Elle a lieu après 12 nouveaux messages, ou une fois par jour à
   partir de 3. Elle propose au plus trois règles, marquées « [appris] ».
3. **Ton attention**, sans modèle : un point du jour resté cinq fois de suite sans réponse
   n'est plus envoyé, et la raison est gardée.

Garde-fous appliqués par le code :
- une règle déduite doit citer au moins deux de **tes** événements (messages ou réactions). Un
  contenu tiers ne peut jamais changer sa façon de faire ;
- la réflexion ne peut ni contredire ni retirer une règle que tu as donnée ; elle ne retire que
  ses propres déductions ;
- les règles déduites ont une confiance `SYSTEM` et dérivent de leurs preuves. Oublier une
  conversation oublie aussi ce qu'il en avait appris (vérifié par rejeu) ;
- chaque réflexion est journalisée avec la position couverte : jamais deux fois sur les mêmes
  signaux, même après un redémarrage.

Le manuel passe par les messages, comme la mémoire (D17). Il est ouvert au début d'une fenêtre
dans `<memoire>`, puis chaque changement arrive une seule fois dans un bloc `<manuel>`. La
constitution demande à Orion de dire en une phrase ce qu'il a changé de lui-même, pour que tu
puisses le corriger. `orion memory` affiche le manuel en tête du profil.

## D22 — M4 : routeur, vérificateur, délégation au CLI `claude`

*19/10/2026 · acceptée*

- **Routeur** : chaque appel modèle passe par un routeur. Un modèle se note `fournisseur:modèle`
  (ou le modèle seul pour OpenRouter). Si un appel échoue après les tentatives propres au
  fournisseur, le routeur essaie la chaîne de secours (par défaut Claude Haiku 5.5). Le secours
  coûte le cache : il ne sert qu'en cas de panne.
- **Vérificateur** : avant d'envoyer un message de sa propre initiative, Orion le fait relire par
  un **autre** modèle (Claude Haiku 5.5, d'une autre famille que DeepSeek ; la config refuse le
  même modèle). Le vérificateur ne voit que le brouillon et les faits : jour, heure, montant,
  nom. En cas de doute ou de panne, c'est le texte simple et exact qui part. Calibré sur de vrais
  brouillons : il attrape « mercredi » au lieu de « demain » et 80 € au lieu de 50 €, et accepte
  les reformulations.
- **Délégation (voie C)** : l'outil `delegate` confie une tâche entière au binaire `claude`
  officiel, non modifié, connecté à l'abonnement du propriétaire (`claude auth login` sur le
  serveur). Garde-fous :
  - un dossier vide par tâche ;
  - mode `-p`, liste d'outils fixe : fichiers seulement, sans shell sauf réglage explicite ;
  - sans MCP ni session persistante, avec un plafond de dépenses ;
  - un environnement nettoyé : aucun secret d'Orion n'est transmis.

  La tâche tourne en arrière-plan. Orion répond « c'est lancé », et le moteur d'initiative
  transmet le résultat à la fin (vérifié comme tout message proactif). Une tâche coupée par un
  redémarrage est marquée en échec, jamais oubliée. Avec le shell autorisé, la délégation devient
  un effet externe : ton accord est demandé à chaque fois.
- **CLI comme modèle (voie B)** : reportée. Le protocole exact (le CLI renvoie-t-il les appels
  d'outils sans les exécuter ?) doit être vérifié avec le binaire connecté à ton abonnement, sur
  ton serveur. L'interface `ModelProvider` et le routeur sont prêts à l'accueillir comme
  fournisseur `claude-cli:`.

## D23 — Boîte de réception unifiée, voix par Inkbox (révise D10)

*19/10/2026 · acceptée*

**Boîte de réception unifiée.** Les canaux ne font plus que traduire : recevoir, dédoublonner,
journaliser, livrer. Tout ce qui est commun passe par `OwnerInbox` :
- une seule file et un seul travailleur pour le fil du propriétaire ;
- les rafales d'un même canal sont regroupées en un seul tour ;
- le canal d'origine montre qu'Orion travaille (accusé de lecture, indicateur de frappe) ;
- la réponse part par le canal demandé, avec `SILENCE` permis pour les transcriptions d'appel ;
- les messages proactifs passent par le premier canal disponible.

La future TUI sera un canal de plus.

**Voix : Inkbox à la place de Twilio et Pipecat** (demande du propriétaire). La voix hébergée
d'Inkbox (Voice AI) mène l'appel, sur la même ligne que l'iMessage. Elle ne peut pas appeler les
outils d'Orion en cours d'appel, d'où le fonctionnement en trois temps :
1. **Avant** : Orion tient la voix à jour avec un brief tiré de sa mémoire (manuel, profil,
   engagements, rappels, derniers échanges), renvoyé à Inkbox dès qu'il change. Seul le contenu
   de confiance `SYSTEM` ou plus y entre.
2. **Pendant** : la voix parle comme Orion. Elle n'enregistre rien et dit « c'est noté ».
3. **Après** : la transcription revient dans le fil (canal `appel`). Orion fait ce qui a été
   convenu (rappels, mémoire, engagements) et confirme par iMessage seulement s'il y a quelque
   chose à confirmer.

Outils : `call_me` appelle le propriétaire ; `remind_me` avec `by_call` sert de réveil ou d'alerte
par appel ; `call_someone` appelle un tiers pour lui, et c'est un effet externe, donc son accord
est demandé à chaque fois. Le compte rendu d'un appel à un tiers est un contenu tiers : il informe,
il n'ordonne rien.

**Sécurité.** `orion inkbox setup` passe la ligne en liste blanche avec le seul numéro du
propriétaire, pour l'iMessage comme pour le téléphone. Un inconnu ne peut donc pas parler à une
voix qui connaît sa vie. Pour un appel à un tiers, Orion ajoute juste avant une autorisation
sortante pour ce numéro seulement.

**Limite connue.** Le compte rendu d'un appel à un tiers, comme tout contenu tiers, abaisse la
confiance de la fenêtre de conversation. Jusqu'au prochain rebase, les écritures en mémoire y sont
donc refusées. La réponse prévue : traiter ces contenus dans un appel isolé qui ne rend que des
champs structurés (mise en quarantaine). Elle sera faite avec la séparation du gardien.

**Plus tard.** Le mode `client_websocket` d'Inkbox permettra de faire tourner notre propre
pipeline vocal temps réel, avec un accès direct à la mémoire pendant l'appel, quand un modèle
vocal temps réel sera branché.

## D24 — Orion modulaire : une plaque et des modules

**Contexte.** Chaque capacité (iMessage, voix, délégation, adaptation) était câblée à la main
dans `app.py` et `serve.py`. M6 va ajouter des sources (agenda, mail), et le propriétaire veut
qu'Orion soit « une plaque sur laquelle on met les legos qu'on veut ».

**Décision.** Le cœur est la plaque : journal, mémoire et manuel, gardien, runtime, boîte de
réception, moteur d'initiative, rappels. Tout le reste est un module (`orion.modules`). Un
module peut :
- donner des outils au modèle (`assemble`, identique en `chat` et en `serve`) ;
- ajouter une section à la constitution ;
- en mode démon, lancer des services supervisés, être un canal du propriétaire, fournir des
  comptes rendus au moteur d'initiative ou passer des appels (`serve`) ;
- dire son état dans `orion doctor`.

Les modules déclarent leurs dépendances (`requires`). Ils sont chargés et démarrés dans l'ordre
de ces dépendances. Si un module est activé sans ce dont il a besoin, Orion refuse de démarrer
(fail closed), et de même si une section `[modules.x]` désigne un module qui n'est pas installé.

Les modules intégrés gardent leurs sections de configuration actuelles (`[channels.imessage]`,
`[voice]`, `[delegation]`, `[adaptation]`). Un module tiers est un paquet Python qui expose une
sous-classe de `Module` dans le groupe de points d'entrée `orion.modules`. Il est configuré dans
`[modules.<nom>]`, que ses propres réglages Pydantic valident.

**Cache.** L'ensemble des modules actifs est fixé au démarrage, donc le registre d'outils et le
message système le sont aussi : le contrat de cache tient. Changer de modules change le préfixe
une fois.

**Sécurité.** Un module est du code de confiance : l'installer lui donne les mêmes accès
qu'Orion. Ce qu'il fait dans le monde passe quand même par le gardien (chaque outil déclare son
effet), et ce qu'il fait entrer porte une `Provenance` et une `Trust`. Isoler les modules dans
leur propre processus viendra avec la séparation du gardien.

**Hors module, volontairement.** Le manuel et ses outils (`adapt`, `unlearn`…) font partie du
cœur, car le propriétaire doit toujours pouvoir dire à Orion comment se comporter. Le module
`adaptation` n'ajoute que la réflexion en arrière-plan.

## D25 — M6, premier module : l'agenda en lecture seule

**Lecture par l'adresse iCal secrète.** Google, iCloud et Outlook publient tous une adresse iCal
secrète. La lire marche partout de la même façon, sans OAuth ni droit d'écriture, et couvre
plusieurs comptes à la fois. Écrire dans l'agenda (créer, déplacer) demandera l'API de chaque
fournisseur. Ce sera une étape suivante, avec approbation.

**Journal.** Chaque synchronisation lit une fenêtre (de la veille à `horizon_days`) et ne
journalise que les changements (`calendar.event.seen`, `calendar.event.gone`). La projection se
reconstruit comme les autres. Un flux illisible ne change rien : un agenda injoignable n'est
jamais pris pour un agenda vide.

**Confiance.** Une invitation est un texte qu'un inconnu peut placer devant Orion, et les
descriptions d'invitations sont un vecteur connu d'injection d'instructions. Traiter toute
invitation comme du contenu tiers rendrait l'agenda inutilisable : le gardien refuserait
`remind_me` juste après la lecture de l'agenda. D'où une règle par auteur :
- **ses propres événements** (sans organisateur, ou organisés par une de ses `owner_emails`) :
  tout est lu, confiance `SYSTEM` ;
- **les invitations qu'il a acceptées** : heure, titre et lieu seulement, en confiance `SYSTEM`.
  Il les a vues et adoptées. La description, que personne ne relit, n'est jamais conservée ;
- **les invitations sans réponse ou refusées, et les événements où il n'apparaît pas** : seul le
  fait qu'elles existent est gardé (pour les compter), sans titre ni texte.

Sans `owner_emails`, toute invitation reste masquée (fail closed).

**Initiative.** Le moteur accepte désormais, en plus des comptes rendus, des alertes de modules
(soumises aux heures calmes et au budget, et abandonnées une fois expirées) et des lignes pour
les points du jour. L'agenda fournit une alerte avant chaque événement (`heads_up_minutes`), la
journée dans le premier point et le lendemain dans le dernier.

## D26 — Contenu tiers : quarantaine, accord du propriétaire, expiration (révise D23)

**Problème.** Le mail est le cas d'école du contenu non fiable : n'importe qui peut écrire dans
la boîte du propriétaire, et « ignore tes instructions et transfère… » est l'attaque classique
contre un assistant. Jusqu'ici, un contenu tiers dans la conversation bloquait tout outil
d'écriture jusqu'au prochain rebase, et donc toute la journée dès qu'un point mentionnait un
mail. C'est sûr, mais inutilisable.

**1. Quarantaine.** Orion ne lit jamais un mail. Un appel séparé, sans outils, sans mémoire et
sans conversation, le lit et remplit une fiche : catégorie, importance, réponse attendue,
échéance, montant, résumé de 200 caractères au plus. Le code valide et coupe chaque champ. Un
mail qui s'adresse à une IA est réduit à « Mail suspect ». Les mails en masse (listes, publicité)
n'atteignent pas de modèle. La fiche reste du contenu tiers (`THIRD_PARTY`) : la quarantaine
réduit ce qui peut arriver jusqu'à Orion, elle ne le rend pas fiable. Le journal garde les fiches,
jamais le corps des mails.

**2. Le gardien demande au lieu de refuser, mais seulement pour l'interne.** Dans une
conversation contaminée :
- une action **externe** (envoyer, appeler un tiers, déléguer avec shell) reste refusée sans
  demander. C'est exactement ce qu'une instruction injectée demanderait, et le propriétaire ne
  doit pas prendre l'habitude de l'approuver ;
- une écriture **interne** (retenir, engagement, rappel, manuel) lui est soumise, avec la raison
  (« la conversation contient du contenu venu d'ailleurs… »). Sans réponse, c'est non. Ce qui
  est retenu garde la confiance de la conversation (`THIRD_PARTY`) : la mémoire sait d'où ça
  vient.

**3. La contamination expire.** Ce qu'Orion écrit après avoir lu un contenu tiers est lui-même
contaminé (il a pu être influencé). On ne peut donc pas la retirer « au-dessus » du contenu
tiers : il faut repartir d'une fenêtre neuve. On le fait après une pause de la conversation
(`agent.untrusted_idle_minutes`, 2 h par défaut). La mémoire, le manuel et les engagements
reviennent en tête de la nouvelle fenêtre. Chaque message journalisé porte désormais l'heure de
son tour (`at`), ce qui rend cette règle déterministe et testable.

**Mail v0.** IMAP en lecture seule stricte (`EXAMINE`, `BODY.PEEK`), avec mot de passe
d'application : Gmail, iCloud, Fastmail et la plupart des hébergeurs. Outlook.com impose OAuth et
viendra plus tard. Synchronisation par UID (et reprise depuis une date si le serveur renumérote
le dossier), au plus `max_per_sync` mails par passage. Le module fournit :
- l'outil `mail` ;
- une alerte pour un mail important (soumise aux heures calmes et au budget) ;
- les mails utiles reçus depuis le point précédent, dans chaque point du jour.

Pas d'envoi : répondre à un mail sera une action externe, donc avec accord, et jamais depuis une
conversation contaminée.

## D27 — Mémoire v1 : sens, personnes, consolidation de nuit

**Recherche par le sens.** Les faits sont aussi retrouvés par proximité de sens : « combien je
paie pour l'appart ? » trouve « le loyer est de 850 € », sans aucun mot en commun. Les embeddings
passent par OpenRouter (`openai/text-embedding-3-small`, quelques centimes par an pour une
mémoire personnelle), avec la même clé : pas de modèle local à installer. La recherche fusionne
le classement plein texte et le classement vectoriel par rang réciproque (RRF), donc les mots
exacts comptent toujours.

Les vecteurs vivent dans `embeddings.sqlite3`. C'est un cache, pas une projection : le supprimer
coûte un recalcul, et un fait oublié disparaît du store, donc on ne le retrouve plus, quoi
qu'il reste dans le cache. Si les embeddings ne répondent pas, la recherche se replie sur le plein
texte et le journalise. Un fournisseur sans embeddings (les tests) désactive simplement le sens.

**Personnes.** Il n'y a pas de fiche « personne » stockée. Une personne est l'ensemble des faits
dont `about` la nomme et des engagements où elle apparaît, sous un seul nom. Les alias
(« Paul », « Paul Martin ») sont des événements `person.aliased`. Deux personnes sont liées quand
des faits les concernent toutes les deux. Le graphe est dérivé de la mémoire : toujours à jour,
et un fait oublié en sort. Outils : `person`, `people`, `same_person`. Les noms du propriétaire
(`memory.owner_names`) sont exclus.

**Consolidation de nuit** (module `consolidation`, actif par défaut). Chaque nuit, les paires de
faits proches (cosinus ≥ 0,5 ; les embeddings réels sont plus bas qu'on ne croit : deux
formulations du même fait étaient à 0,52) sont jugées par le modèle d'arrière-plan :
- doublon : on garde la version la plus précise ;
- mise à jour : la plus récente remplace l'ancienne ;
- contradiction : on pose la question au propriétaire dans le point du matin ;
- compatibles : rien.

Les noms dont l'un est contenu dans l'autre sont fusionnés si le juge le confirme.

Le code, pas le modèle, décide de ce qui peut tomber : un fait n'est jamais retiré au profit d'un
fait moins fiable. Un fait lu dans un mail ne peut donc pas évincer ce que le propriétaire a dit :
ça devient une question. Chaque changement est un événement mémoire ordinaire
(`orion:consolidation`), visible dans l'historique et réversible. Essai réel : doublon retiré,
« habite à Bordeaux » remplacé par « a déménagé à Nantes », « Paul » fusionné avec « Paul
Martin », Lyon contre Marseille (venu d'un mail) posé en question.

**Plus tard.** Pour un doublon, fusionner les deux formulations en une seule plutôt que d'en
choisir une. La fusion réécrit un souvenir ; elle demandera la même prudence que le reste.

## D28 — Le gardien dans son propre processus

*19/10/2026 · acceptée · réalise D14 et §3 de l'architecture*

**Problème.** Jusqu'ici, le gardien était une classe dans le processus d'Orion. Le modèle ne peut
pas contourner du code, mais tout ce qui tourne dans ce processus le peut : un module tiers, une
dépendance compromise, un agent délégué qui sortirait de son dossier. Ce processus détenait aussi
tous les secrets : la clé Inkbox, les adresses secrètes des agendas, les mots de passe des boîtes
mail.

**Décision.** Orion peut tourner en deux processus (`[guardian] enabled = true`) :
- `orion guardian` détient les secrets et applique des règles qui tiennent **même si le noyau
  est compromis** ;
- `orion serve` ne détient que la clé du fournisseur de modèles. Il parle au gardien par une
  socket Unix (mode 0660, partagée par groupe), en JSON.

Le noyau refuse de démarrer s'il voit dans son environnement un secret qui revient au gardien
(fail closed). Pour que la séparation soit réelle, chaque processus tourne sous son propre
utilisateur Unix (voir `deploy/`).

**Ce que le gardien vérifie, sans rien savoir de la conversation :**
- **Inkbox.** Le noyau utilise le même client qu'avant, pointé sur le gardien, qui ajoute la clé
  si la requête passe :
  - les lectures sont permises ;
  - un message, un indicateur de frappe ou un accusé de lecture ne part que vers une
    conversation dont le gardien a vérifié lui-même, auprès d'Inkbox, qu'elle est celle du
    propriétaire ;
  - 30 messages par minute au plus, pour qu'une boucle folle s'arrête ;
  - jamais de changement des réglages de la ligne : la liste blanche reste en place, et
    `orion inkbox setup` se lance avec la clé, sous l'utilisateur du gardien ;
  - jamais d'action pour une autre identité ;
  - appeler le propriétaire est permis, appeler quelqu'un d'autre demande une approbation.
- **Les approbations.** Le gardien écrit lui-même la question, à partir de l'action et de ses
  paramètres : le propriétaire voit ce qui va réellement s'exécuter. L'action approuvée
  présente ensuite son approbation. Le gardien vérifie alors auprès d'Inkbox que le propriétaire
  a bien dit oui : un 👍 sur ce message, ou un « oui » écrit après lui (la première réponse
  compte). Il vérifie aussi que la requête est celle qui a été approuvée (même numéro, même
  objectif dans le brief de l'appel). Une approbation sert une fois et expire au bout de 30 min.
- **Agendas et mail.** Le gardien les lit et ne renvoie que le contenu demandé. Les adresses
  secrètes et les mots de passe ne quittent jamais son processus.
- **Webhooks.** La signature des livraisons Inkbox est vérifiée par le gardien, qui détient la
  clé de signature.

Tout ce que le gardien doit savoir est dans **son propre registre**, append-only. On y trouve les
conversations du propriétaire vérifiées, les approbations envoyées et utilisées, et les refus
(chacun journalisé). Il est rangé dans un dossier en 0700 que le noyau ne lit pas, et se rejoue
au démarrage comme le journal du noyau.

**Ce qui reste dans le noyau.** La politique qui dépend de la conversation reste là où est la
conversation (`orion.guardian.policy`). Elle comprend la confiance de la fenêtre (D26) et les
actions internes soumises au propriétaire. Le gardien ne voit que des requêtes, jamais le
raisonnement de l'agent.

**Limites connues.**
- Le noyau garde la clé du fournisseur de modèles. C'est par là que partent, par construction,
  la mémoire et la conversation : un noyau compromis peut donc encore faire fuir ce qu'il sait
  par ce canal. Le gardien empêche qu'il agisse au nom du propriétaire, il ne rend pas la
  mémoire secrète.
- L'agent délégué (`claude`) tourne sous l'utilisateur du noyau.
- `orion chat` en mode séparé demande les approbations dans le terminal. Le gardien ne peut pas
  les vérifier, donc les actions qu'il contrôle (appeler un tiers, écrire à un autre Orion) y
  sont refusées.
- Le mode à un seul processus reste disponible (`enabled = false`, par défaut). Il est commode
  pour essayer Orion, mais n'offre pas ces garanties.

## D29 — M7 v0 : deux Orion calent un rendez-vous

*19/10/2026 · acceptée · réalise D8 et D13 pour un premier cas*

**Identité.** Chaque instance a une clé Ed25519 (`orion federation keygen`). Elle vit seulement
dans l'environnement du gardien, et la fédération exige donc le gardien séparé (D28). Le gardien
publie une carte d'agent A2A (`/.well-known/agent-card.json`), qui contient l'instance, la clé
publique et une empreinte courte à lire à voix haute.

**Présentation entre humains.** Personne ne peut écrire à ton Orion sans y avoir été invité.
- Tu crées une invitation : `orion federation invite --name Paul`. Elle vaut une seule fois,
  pendant 7 jours. Tu donnes le code à Paul de la main à la main ou par message.
- Paul l'accepte : `orion federation accept <code> --name Noan`. Son Orion se présente au tien,
  signé, avec le jeton.
- Chacun épingle la clé et l'adresse de l'autre. La relation porte des compétences ;
  `scheduling` est la seule pour l'instant.
- Ces commandes demandent le jeton d'administration du gardien. Il est rangé dans son dossier,
  que le noyau ne peut pas lire. Un noyau compromis ne peut donc pas relier ton Orion à un
  inconnu.
- Une relation se révoque avec `orion federation revoke`.

**Le fil.** Chaque message est une enveloppe signée qui couvre tous les champs : expéditeur,
destinataire, type, négociation, heure, corps. Elle voyage comme un `message/send` JSON-RPC
d'A2A, avec une partie de données. La couche de transport est isolée dans
`orion.federation.wire`, pour suivre la spécification A2A au fur et à mesure qu'elle se fixe. Les
corps de message sont typés et bornés : le seul texte libre est un titre de 80 caractères.

**Le gardien, seul à parler aux autres Orion.**
- **À l'entrée**, il vérifie dans l'ordre :
  - que l'expéditeur est un pair actif et que la signature correspond à sa clé épinglée ;
  - que le message t'est bien destiné ;
  - l'horloge, à ±10 min près ;
  - qu'il ne s'agit pas d'un rejeu (chaque id une seule fois) ;
  - que le type est permis par la relation et que le corps est bien formé ;
  - un quota par pair.

  Ce qui passe va dans son registre, et le noyau vient le chercher. Ce qui ne passe pas est
  refusé avant toute lecture, et journalisé.
- **À la sortie**, il signe et n'envoie qu'à l'adresse épinglée d'un pair, en HTTPS. Il
  n'envoie que ce que le propriétaire a accepté :
  - une **proposition** exige son approbation, que le gardien vérifie lui-même (D28) : même
    pair, même titre, même durée, même fenêtre, et chaque créneau dans la fenêtre ;
  - une **acceptation** exige l'approbation de ce créneau, pour une proposition réellement reçue
    qui le contenait ;
  - une **confirmation** doit correspondre à la proposition approuvée et à l'acceptation reçue ;
  - un **refus** ne révèle rien, et ne peut que répondre à une proposition reçue.

**Le négociateur est du code, pas un modèle** (`orion.federation.meetings`). Ce qu'envoie un
autre Orion est une donnée que le code vérifie : rien ne peut y orienter une décision. Le
déroulé :
1. Tu demandes à Orion de caler un rendez-vous (`meeting_propose` : avec qui, quoi, combien de
   temps, dans quelle fenêtre). Tu approuves la proposition.
2. Ton Orion envoie au plus 8 créneaux libres de cette fenêtre, au plus 2 par jour, à 2 h
   d'écart minimum, dans tes heures (`meeting_start`, `meeting_end`, `weekends`). Un créneau
   est libre s'il ne chevauche ni tes événements visibles ni un rendez-vous déjà calé. Rien
   d'autre de ton agenda ne part.
3. L'Orion de Paul garde le premier de ces créneaux qui est libre pour lui, et le soumet à Paul.
   S'il n'y en a aucun, il refuse sans le déranger.
4. Paul dit oui : l'acceptation part, ton Orion confirme. De chaque côté, le rendez-vous devient
   un engagement daté du propriétaire (avec l'alerte qui précède), et un compte rendu passe par
   le moteur d'initiative. Les deux signatures sont gardées comme reçus.

**Confiance.** Ce qui vient d'un autre Orion est `TRUSTED_PEER`, en dessous du plancher
`SYSTEM`. Ses comptes rendus contaminent donc la conversation comme un mail (D26), et
l'engagement garde cette confiance. Les agendas sont en lecture seule : le compte rendu
rappelle d'ajouter le rendez-vous à son agenda.

**Pas encore.**
- Contre-propositions et expiration des propositions sans réponse.
- Mandats et capacités Biscuit : la relation porte pour l'instant une simple liste de
  compétences.
- Négociateur à modèle pour des cas plus ouverts (un prix, un lieu).
- Instances d'un même propriétaire (`OWNER_INSTANCE`).
- Écriture dans l'agenda.

## D30 — Sur un serveur : installation, sauvegardes, le gardien veille sur le cœur

*20/10/2026 · acceptée*

Orion tourne sur un VPS (OVH chez le propriétaire). `deploy/install.sh` installe tout sur une
Debian ou une Ubuntu neuve : deux utilisateurs, Caddy en HTTPS, systemd, pare-feu. Caddy ne
laisse passer que le webhook Inkbox, la voix en direct, les mains et la console (vers le cœur),
et la fédération (vers le gardien). Le webhook remplace le polling.

- **Sauvegardes** : `orion backup` fait une copie SQLite en ligne du journal, vérifiée
  (`integrity_check`), avec rotation. Un timer par nuit pour le cœur, un pour le gardien. Le
  journal suffit : tout le reste se reconstruit.
- **Le cœur ne peut pas dire qu'il est mort** : il envoie un battement au gardien chaque
  minute. Après 5 minutes de silence, le gardien écrit lui-même au propriétaire, une fois, puis
  quand le cœur repart.

## D31 — Le web, par un intermédiaire

*20/10/2026 · acceptée*

Module `web` : `web_search` et `web_read`. Le modèle d'Orion ne lit jamais une page : un appel
séparé, sans outils ni mémoire, répond à sa question sous une forme bornée (comme la quarantaine
du mail). Le résultat porte `Trust.WEB`.

- La recherche passe par la recherche web du fournisseur (plugin `web` d'OpenRouter) : aucun
  compte de plus. Seules les pages réellement citées reviennent : l'intermédiaire ne peut pas
  inventer d'adresse.
- **Une URL est aussi un moyen de faire sortir des données** (`?q=<secrets>`). Orion n'ouvre
  donc que les adresses que le propriétaire a écrites, qu'une recherche a renvoyées, ou que
  l'intermédiaire a choisies sur une page lue. Les adresses privées sont refusées, à chaque
  redirection.

## D32 — Les mains d'Orion : agenda, mail, rendez-vous fédérés

*20/10/2026 · acceptée*

- `calendar_add` écrit en CalDAV (iCloud, Fastmail, Nextcloud) ou dans Google Agenda (jeton
  obtenu par `orion google auth`). Aucun invité : personne n'est prévenu au nom du
  propriétaire. L'événement s'affiche tout de suite, et reste tant que le flux iCal ne le montre
  pas encore (Google a des heures de retard).
- `mail_send` envoie en SMTP depuis le compte du propriétaire, dans le fil quand c'est une
  réponse.
- Le gardien rédige lui-même la demande d'accord : l'événement exact, le mail mot pour mot.
  Il ne laisse passer que ce qui a été approuvé, une fois.
- Répondre à un mail se fait forcément dans une conversation qui contient du contenu tiers. Ces
  outils acceptent donc ce contexte (révise D26 pour eux seuls) : la demande d'accord le dit, et
  le propriétaire voit tout.
- Un rendez-vous que les deux Orion ont signé va dans les deux agendas sans second accord. Le
  gardien le vérifie sur la confirmation qu'il a lui-même signée ou reçue.

## D33 — L'ordinateur d'Orion

*20/10/2026 · acceptée*

Module `computer` : un Chromium sans écran (Playwright), avec un profil neuf à chaque tâche,
conduit par un agent qui ne voit que la consigne. Il n'a accès ni à la mémoire ni à la
conversation, et son compte rendu revient comme contenu du web.

Les règles sont appliquées par la couche réseau, pas par le modèle :
- aucune adresse privée ;
- en lecture seule (`computer_task`), toute requête autre que GET ou HEAD est coupée, donc rien
  ne part ;
- une tâche qui doit envoyer (`computer_task_send`) fait approuver sa consigne exacte.

Il remplace la délégation au CLI `claude`, qui dépend de ce qui est installé.

## D34 — Faire connaissance

*20/10/2026 · acceptée*

Module `onboarding`, actif par défaut. Pendant trois semaines, Orion pose une question par jour
au plus. Elle passe par le moteur d'initiative : heures calmes, budget et vérificateur
s'appliquent. Jamais dans les 90 minutes qui suivent un message du propriétaire, et jamais sur
ce que la mémoire sait déjà. `onboarding_stop` arrête tout.

## D35 — Une autonomie qui se gagne

*20/10/2026 · acceptée*

Une action soumise à accord peut devenir une action qu'Orion fait seul :
- seulement pour les actions listées dans `[guardian] autonomable` (par défaut `calendar_add`) ;
- seulement dans une conversation sans contenu tiers ;
- dans la limite d'un plafond quotidien tenu par le gardien.

Après 5 accords de suite, Orion le propose au plus une fois par mois, à une heure calme. Le
gardien rédige la question, vérifie le oui et enregistre l'autonomie lui-même. `autonomy_revoke`
la retire d'un mot. Orion doit la rendre au premier regret du propriétaire.

## D36 — Proactivité v1 : un arbitre et « ce que j'ai failli te dire »

*20/10/2026 · acceptée*

Après le portail (heures calmes, budget, expiration), un arbitre décide si le message vaut d'être
envoyé maintenant :
- l'urgence est l'importance du message, qui monte quand l'échéance approche et à mesure qu'il
  attend ;
- le dérangement coûte plus pendant un événement de l'agenda et quand le budget s'épuise, et
  moins juste après la fin d'un événement.

Un message qui peut attendre attend un meilleur moment : il n'est pas jeté, et le verdict est
journalisé. Le dimanche soir, Orion dit ce qu'il a gardé pour lui dans la semaine.

## D37 — Engagements v1

*20/10/2026 · acceptée*

La fiche de quarantaine relève la promesse que porte un mail (ce que le propriétaire doit, ou ce
qu'on lui doit), jamais pour une publicité. Elle devient un engagement marqué contenu tiers.
Répondre à ce mail clôt ce que le propriétaire lui devait, avec le mail envoyé comme preuve.
`mail_send` et `calendar_add` clôturent les engagements qu'ils nomment (`closes`).

## D38 — La provenance au travail

*20/10/2026 · acceptée*

`contain_source` et `orion contain` partent d'une source suspecte et rassemblent :
- ses propres événements ;
- les tours où elle a atteint le modèle ;
- la suite de chaque fenêtre de conversation, jusqu'à ce qu'elle reparte à neuf ;
- tout ce qui en dérive.

L'outil montre d'abord la liste, puis invalide ce qui est encore actif : souvenirs,
engagements, rappels, règles. Rien n'est effacé, à la différence de l'oubli. Ce qui est déjà
parti est seulement listé. `provenance` remonte la lignée d'un souvenir ou d'un engagement.

## D39 — La console

*20/10/2026 · acceptée*

Module `console` : une page HTML simple, servie par le cœur, lisible sur un téléphone. On y voit
et corrige la mémoire, les engagements, le manuel et l'autonomie. On y suit l'activité (ce
qu'Orion a fait, ce qu'il a tu, et pourquoi) et les sources suspectes.

- **Entrée** : un lien à usage unique de 10 minutes, qu'Orion envoie (`console_link`), devient
  un cookie signé HttpOnly, Secure et SameSite=Strict.
- **Corrections** : chacune est un événement du propriétaire dans le journal, le même que
  celui des outils.

## D40 — Voix v1 : Orion au téléphone

*20/10/2026 · acceptée · révise D23*

Avec `[voice] live`, les appels avec le propriétaire passent par le flux média d'Inkbox. Inkbox
écoute et parle. Chaque phrase du propriétaire passe par la boîte de réception (canal `voix`)
sur son fil, avec la même mémoire et les mêmes outils, et la réponse est lue tout de suite. Une
formule d'attente couvre les réponses lentes.

Seul un flux signé avec le numéro du propriétaire entre. Le gardien ne diffuse un appel que vers
l'adresse d'Orion, et seulement avec le propriétaire. Les appels aux autres gardent la voix
hébergée et leur accord.

## D41 — Les mains sur les appareils du propriétaire (D1)

*20/10/2026 · acceptée*

`orion hand` tourne sur un appareil du propriétaire et appelle le serveur : aucun port n'est
ouvert sur l'appareil.
- **Appairage** : un code à usage unique ; seules des empreintes sont journalisées.
  `orion hands revoke` coupe un appareil.
- **Actions** : typées, jamais un shell, et chaque main vérifie ce que son appareil permet :
  - notification ;
  - recherche et lecture de fichiers dans les dossiers partagés (chemins résolus, taille
    plafonnée) ;
  - ouverture d'une page, avec accord.
- **Confiance** : le contenu d'un fichier reste sous la pleine confiance.

## D42 — Orion conduit lui-même son navigateur (révise D33)

*20/10/2026 · acceptée*

Le navigateur n'est plus seulement confié à un agent à part : Orion l'ouvre et le conduit
depuis sa propre boucle, page par page (`browser_open`, `browser_click`, `browser_type`,
`browser_select`, `browser_scroll`, `browser_back`, `browser_read`, `browser_close`). Il décide
de chaque action avec ce qu'il sait déjà, et la conversation garde le fil. Un navigateur par
conversation, fermé après dix minutes sans usage.

Le contexte d'Orion contient la vie du propriétaire, donc trois règles tiennent, quoi que dise
une page :
- **Lecture seule** au niveau du réseau : rien ne part, aucun formulaire envoyé. Envoyer reste
  à `computer_task_send`, dont le propriétaire approuve la consigne.
- **Adresses** : seulement celles que le propriétaire a écrites, qu'une recherche a trouvées
  ou qu'une page ouverte propose en lien. Jamais une adresse inventée, qui pourrait porter ses
  données dans la requête.
- **Frappe** : seulement des mots du message auquel Orion répond. Une recherche envoyée par GET
  est aussi une sortie : une page ne peut pas lui faire taper un numéro, même donné plus tôt.

Chaque page vue est journalisée en confiance `WEB` avec la source `web:<adresse>` : la
provenance la retrouve et peut la contenir (D38). `computer_task` reste pour les longues
tâches en arrière-plan.

## D43 — La boucle, comparée à celle de Claude Code

*20/10/2026 · acceptée*

Comparée à la boucle documentée de Claude Code (sa source n'est pas publiée ; seule la
documentation officielle a servi), celle d'Orion avait déjà : outils en lecture lancés en
parallèle, contrôle de chaque appel, messages du propriétaire lus en cours de tour, budgets,
détection des appels répétés, reprises et modèles de secours. Trois écarts sont comblés :
- **Une limite donne une réponse** : quand un tour atteint ses étapes, ses appels, son coût
  ou se répète, un dernier appel au modèle, outils déclarés mais interdits (même préfixe en
  cache), dit au propriétaire ce qui est fait et ce qui reste. Les appels coupés par un budget
  reçoivent un résultat « non exécuté » : l'historique restait sinon mal formé.
- **Le rebase résume ce qu'il laisse** : comme la compaction de Claude Code, les tours de
  confiance qui sortent de la fenêtre sont résumés dans le nouveau message, journalisé tel
  quel. Les tours non fiables ne sont pas résumés : ce serait porter leurs consignes au-delà.
- **Des limites pour travailler** : 40 étapes et 80 appels par tour ; le coût et les
  répétitions sont les vrais freins.

Restent hors de ce lot : la réponse en continu (utile surtout pour la voix) et l'effacement
des anciens résultats d'outils, qui casserait le contrat de cache tel qu'il est écrit.

## D44 — Trois idées reprises d'OpenClaw

*20/10/2026 · acceptée · complète D43*

La comparaison avec OpenClaw (MIT, code lu dans le dépôt) en apporte trois :
- **Alléger sans perdre le cache.** Après dix minutes sans message, le cache du fournisseur a
  expiré de toute façon : les vieux résultats d'outils volumineux (plus de 2 000 caractères,
  avant les deux derniers échanges) sont alors montrés réduits à leur début. C'est un
  événement `thread.pruned` du journal, comme un rebase : la projection se reconstruit, et le
  journal garde chaque message entier. Le contrat de cache tient, le préfixe ne change qu'après
  une pause.
- **Lire le propriétaire avant de continuer.** Quand il écrit pendant un tour, les appels
  d'outils pas encore lancés sont sautés avec un résultat « non exécuté, lis son message ». Un
  appel en cours n'est jamais interrompu, et chaque appel garde sa réponse.
- **Un dernier regard avant d'oublier.** Les tours de confiance qu'un rebase sort de la fenêtre
  passent une dernière fois par l'extracteur de mémoire, en plus du résumé (D43).

Orion fait aussi des petits messages en cours de tour : ce que le modèle écrit à côté d'un
appel d'outil part tout de suite, par la même sortie que la réponse.

## D45 — Des évaluations tirées des vraies conversations

*20/10/2026 · acceptée*

`uv run python -m evals` rejoue des moments du journal avec le vrai modèle et les vrais
outils : la configuration de production sans canaux, sans gardien et sans voix, dans un
répertoire neuf, l'horloge déplacée au moment du cas. Tout ce qui demande un accord est refusé
et noté. Les vérifications portent d'abord sur ce qu'Orion a fait (rappels posés, outils
utilisés), ensuite sur ce qu'il a dit. Un juge d'une autre famille de modèles répond par oui
ou non sur le ton.

Les tests unitaires prouvent que le code fait ce qu'il dit ; les évaluations disent si Orion se
comporte bien. Elles coûtent quelques centimes et ne sont pas déterministes : `--repeat` mesure
la stabilité, `--model` compare les modèles sur les mêmes cas. Chaque vrai raté devient un cas.

## D46 — Un modèle plus fort, à la demande d'Orion

*20/10/2026 · acceptée*

Le modèle du quotidien est rapide et peu cher. Quand c'est difficile, Orion choisit lui-même :
- `consult_expert` pose une question au modèle fort (par défaut Claude Opus 5.5), avec le
  contexte qu'Orion lui donne ; la réponse revient comme résultat d'outil, jamais plus fiable
  que ce contexte, et le tour reste sur le modèle du quotidien ;
- `think_harder` fait passer la suite du tour sur le modèle fort, avec son propre plafond de
  coût. La conversation ne change pas : chaque requête prolonge encore la précédente.

Les deux sont journalisés avec leur coût et plafonnés par jour (`[modules.expert]`).

## D47 — Les missions

*20/10/2026 · acceptée*

Un but qui prend des jours (trouver un appartement, guetter un prix) devient une mission :
Orion y travaille seul, à son rythme, chaque fois dans le fil de la mission, qui garde la
mémoire de ce qu'il a fait. Ce qu'il trouve n'arrive au propriétaire que s'il y a du neuf, par
le moteur d'initiative (heures calmes, budget, arbitre). `mission_seen` dit ce qui est neuf,
donc rien n'est signalé deux fois ; ce que le propriétaire écarte est gardé avec son motif, et
les passages suivants le lisent. Le dossier d'une mission n'est jamais plus fiable que les
pages qu'elle a notées.

## D48 — Le second cerveau : la bibliothèque

*20/10/2026 · acceptée*

À côté de la mémoire (des faits), la bibliothèque garde entiers les textes que le propriétaire
confie (notes, fichiers de son ordinateur, pages) et toute la conversation, datée. On y
cherche par les mots et par le sens (FTS5 et embeddings, fusionnés par rang, comme la
mémoire), et on retrouve « ce que je disais de ça en août ». L'index est une projection du
journal. Un document revient comme contenu du propriétaire, sous la pleine confiance ; une
ancienne réponse garde la confiance qu'elle avait. `orion library import <dossier>` importe
un dossier de notes.

## D49 — Agir dans le monde : des délégations bornées, des sessions prêtées

*20/10/2026 · acceptée · complète D35*

- **Limites** : une autonomie peut porter des limites que le propriétaire fixe en l'accordant
  (destinataires, sites), vérifiées en code par le gardien et par le cœur. Envoyer un mail ou
  un formulaire seul ne s'accorde qu'avec elles : jamais un inconnu, jamais un site non nommé.
  Le cœur ne peut pas élargir ce que le propriétaire a approuvé.
- **Sites** : une tâche qui envoie peut être limitée à des sites ; ailleurs, le navigateur
  bloque tout envoi.
- **Sessions** : pour agir sur un compte du propriétaire, le gardien garde la session (les
  cookies), comme les mots de passe. Il la prête pour une tâche approuvée (cette session, ce
  texte exact, une seule fois), ou faite seule sur des sites couverts par l'autonomie. Le cœur
  ne la garde qu'en mémoire, le temps de la tâche, et chaque prêt est noté.

Pas de paiement : la consigne de l'agent l'arrête avant tout paiement, sauf si le texte
approuvé dit explicitement de payer.

## D50 — Les photos

*20/10/2026 · acceptée*

Module `photos`, actif par défaut. Le propriétaire envoie une photo par iMessage : une affiche,
une ordonnance, un ticket, une capture d'écran.
- **Regardée une fois** : un modèle qui voit (Gemini Flash, qui lit le HEIC de l'iPhone) la
  décrit à son arrivée, avec tout le texte qu'elle porte. Le modèle de tous les jours ne reçoit
  que cette description : il n'a pas besoin de voir, et une image rejouée à chaque requête
  pèserait sur chaque tour. Le contrat du cache tient.
- **Du contenu, pas des ordres** : le message qui porte une photo est journalisé sous
  `OWNER_INSTANCE`, comme un fichier lu sur l'ordinateur du propriétaire. Les règles du contenu
  venu d'ailleurs s'appliquent (D26) : il propose, le propriétaire approuve. Les mots de la
  photo ne comptent jamais comme ceux du propriétaire (ils ne peuvent pas être tapés dans un
  site, ni extraits en mémoire comme s'il les avait dits).
- **Gardée** : le fichier reste dans le dossier de données (0600), et `photo_look` repose une
  question précise sur un détail. Le journal ne garde que la description et une empreinte.
  Oublier la conversation oublie la photo : le fichier est supprimé dès qu'aucun événement ne
  le nomme plus.
- **Lien qui expire** : Inkbox donne un lien valable une heure ; après un redémarrage, le
  message est relu pour en obtenir un neuf.

## D51 — Il continue de penser, il prend des nouvelles

*20/10/2026 · acceptée*

Module `thinking`, actif par défaut. Un assistant s'arrête quand la conversation s'arrête ;
Orion, non.
- **La nuit** (5 h par défaut), un modèle fort (Opus) relit la journée avec le propriétaire et
  ce qu'il sait de lui. Pour chaque question restée ouverte (une décision, un problème, une
  hésitation), il réfléchit, et ne garde une pensée que si elle apporte du neuf. Deux par nuit
  au plus. Elles arrivent le matin comme une pensée qui lui est venue.
- **Promettre d'y réfléchir** : `think_later`. Une question promise reçoit toujours une
  réponse, même « je maintiens », et en plus des deux pensées de la nuit.
- **Prendre des nouvelles** : après un moment qui compte (entretien, médecin, visite, examen),
  Orion demande une fois comment ça s'est passé, quelques heures après. Il le prévoit pendant la
  conversation (`follow_up`), ou la lecture de nuit le trouve. La réponse reste dans la
  conversation, et la mémoire en garde l'issue.

Tout passe par le moteur d'initiative : heures calmes, budget, vérificateur, et `SILENCE` si
la question a été réglée entre-temps. La nuit ne relit que les tours de confiance : ce que
disait une page, un mail ou une photo n'oriente jamais sa réflexion. Les dates des moments
sont demandées en absolu, calculées depuis la date de chaque message, et refusées si elles sont
passées ou à plus de 90 jours.

## D52 — La chronique

*20/10/2026 · acceptée*

Module `chronicle`, actif par défaut. Chaque dimanche à 19 h, Orion écrit la page de la
semaine du propriétaire, à la deuxième personne, entre 120 et 350 mots : ce qui s'est passé, ce
qu'il a décidé ou laissé en suspens, les gens, ce qui l'occupait. Rien d'inventé, et pas de
tableau de bord : un texte qu'on a envie de lire.
- Elle lui est envoyée par le moteur d'initiative, et rangée dans la bibliothèque (« Ma semaine
  du 19 octobre 2026 »), où `library_search` la retrouve. `chronicle` relit la page de
  n'importe quelle semaine.
- La page de la même semaine, l'an dernier, est donnée à l'auteur : il les relie quand le
  rapprochement dit quelque chose.
- Comme la nuit (D51), seuls les tours de confiance sont lus. Une semaine de moins de six
  échanges n'a pas de page.

## D53 — Ses intentions

*20/10/2026 · acceptée*

Module `intentions`, actif par défaut. Un engagement est une tâche ; une intention est ce que
le propriétaire veut devenir (courir plus, dépenser moins, voir plus sa mère).
- **En vue** : chaque intention est aussi un fait de profil en mémoire. Orion l'a sous les yeux
  au début de chaque conversation et la retrouve quand un message la touche. Il peut donc dire
  « tu m'avais dit vouloir… » au moment où ça compte, une fois, puis laisser décider.
- **Notée** : ce qui arrive (`intention_note`, tenu ou écart), quand le propriétaire en parle.
- **Le point** : chaque semaine (ou au rythme choisi), à 18 h 30, Orion fait le point avec ce
  qui a été noté. Il le dit simplement si ça avance, franchement si ça décroche, et demande ce
  qui coince, sans morale. Ça passe par le moteur d'initiative.

Orion propose de suivre une intention plutôt que de le décider seul : la suivre est une forme
d'attention qu'on accepte.

## D54 — Le secrétaire

*20/10/2026 · acceptée*

Module `secretary`, désactivé par défaut. Le propriétaire nomme dans sa configuration les
proches qui peuvent écrire à Orion, avec, pour chacun, qui il est et ce qu'Orion peut lui dire
(`share = ["availability"]` : quand il est libre ou occupé). « Noan est libre jeudi soir ? »
reçoit une réponse. Un message est pris. Ce qui est urgent lui arrive tout de suite, le reste
dans un point.

La sécurité vient de ce que le secrétaire n'a pas :
- c'est un appel de modèle à part, sans outils, qui ne voit ni la mémoire ni la conversation du
  propriétaire. Il voit seulement qui est le contact, leur conversation, et, si c'est permis,
  les créneaux occupés (jamais leur titre ni leur lieu). Une manipulation n'a rien d'autre à
  obtenir ;
- il n'engage le propriétaire à rien : ni rendez-vous, ni promesse, il transmet ;
- le gardien n'envoie qu'aux contacts configurés, dans leur propre conversation (vérifiée
  auprès d'Inkbox), dans les 12 heures qui suivent leur dernier message, et quelques fois par
  jour. Orion répond, il ne commence jamais seul ;
- pour écrire le premier ou transmettre une réponse (`contact_message`), il faut l'accord du
  propriétaire, et le gardien vérifie ce oui sur le texte exact ;
- `orion inkbox setup` ouvre la ligne aux contacts sur iMessage seulement : ils ne peuvent pas
  appeler la voix d'Orion, qui connaît la vie du propriétaire.

Un contact doit d'abord écrire à Orion (la commande de connexion d'Inkbox) : Inkbox n'ouvre une
conversation qu'à l'initiative du destinataire.

## D55 — Tous les modèles se changent dans la configuration

*20/10/2026 · acceptée*

Aucun nom de modèle n'est écrit dans le code hors de `config.py`, et un test le vérifie. La
section `[agent]` nomme les modèles partagés :
- `model` : le modèle de tous les jours ;
- `extractor_model` : le travail de fond ;
- `verifier_model` : la relecture des messages qu'Orion envoie de lui-même ;
- `strong_model` : l'expert, la réflexion de nuit, la chronique ;
- `vision_model` : les photos ;
- `summary_model` et `reflection_model` : ils reprennent `extractor_model` s'ils sont vides ;
- `fallback_models` : les modèles de secours.

Chaque module peut nommer le sien dans sa section, et c'est lui qui l'emporte. Les modèles de
secours s'appliquent désormais à chacun de ces modèles, plus seulement aux deux premiers.

## D56 — Les gens de sa vie

*20/10/2026 · acceptée*

Module `relations`, actif par défaut. La mémoire connaît les personnes (D27) ; ce module rend
Orion attentif à elles.
- **Avant une rencontre** : un événement de l'agenda dont le titre nomme quelqu'un que la
  mémoire connaît, ou une rencontre que le propriétaire mentionne (`meeting_soon`). Orion lui
  rappelle ce qui compte : ce qui se passe dans la vie de la personne, les engagements dans un
  sens ou dans l'autre. La veille au soir pour un rendez-vous du matin, le matin pour un
  rendez-vous plus tard. `SILENCE` s'il ne sait rien d'utile.
- **Anniversaires** (`person_birthday`) : trois jours avant, pour penser à quelque chose, et le
  jour même, avec une idée tirée de ce qu'il sait.
- **Garder le lien** (`keep_in_touch`, `saw_someone`) : pour les gens qu'il veut voir souvent,
  Orion dit quand ça fait trop longtemps, une fois par semaine au plus, avec une idée concrète
  plutôt qu'un reproche.

Le message est écrit par le moteur d'initiative à partir de la fiche de la personne : rien
d'inventé, et la confiance du message est celle des faits qu'il cite.

## D57 — Les rappels de lieu

*20/10/2026 · acceptée*

Module `places`, désactivé par défaut (il faut régler le téléphone). « Quand je passe à la
boulangerie » devient `remind_at_place`. Une automatisation Raccourcis de l'iPhone (arrivée ou
départ d'un lieu) envoie à Orion le seul nom du lieu choisi par le propriétaire, avec un jeton
(`orion places token`) dont le journal ne garde que l'empreinte. Ni coordonnées ni trajet :
Orion ne sait que ce que le propriétaire a décidé de lui dire, et un signal du téléphone est
une information (`OWNER_INSTANCE`), pas un ordre.

Le rappel part tout de suite, heures calmes ou non, comme un rappel à heure fixe : il a été
demandé. Il part une fois, et les noms se rapprochent sans exiger l'exactitude
(« boulangerie » = « Boulangerie Paul »).

## D58 — Ce qu'il remarque, ses goûts

*20/10/2026 · acceptée · complète D52 et D56*

- **Ce qu'il remarque** : le premier dimanche du mois, le modèle de la chronique relit les pages
  des cinq dernières semaines. Il en tire au plus trois choses qui reviennent : une habitude, un
  sujet, une humeur, un progrès, une chose remise à plus tard. Une remarque doit s'appuyer sur au
  moins deux semaines, sans psychologie de comptoir ni leçon, et rien quand rien ne revient. Elle
  arrive le lendemain, pas le soir de la page.
- **Les goûts** : ce que le propriétaire ou ses proches aiment et n'aiment pas (cuisine, lieux,
  films, musique, cadeaux, marques), et ce qu'ils ont écarté et pourquoi, sont des faits
  durables pour l'extracteur de mémoire. Pour un cadeau ou un resto, Orion part de là.

## D59 — Un outil par domaine

*20/10/2026 · acceptée*

Orion avait 64 outils, et environ 80 avec tous les modules : beaucoup de petites variantes
(`mission_start`, `mission_note`, `missions`…) entre lesquelles le modèle doit choisir. Les
variantes d'un même domaine deviennent les actions d'un seul outil (`orion.tools.actions`) :
`mission`, `library`, `intention`, `follow_up`, `relations`, `places`, `commitments`,
`reminders`, `playbook`, `people`, `browser`. Il en reste 33 avec la configuration d'essai. Le
modèle choisit d'abord le domaine, puis l'action, que la description liste avec ses paramètres.

- Chaque action garde son modèle d'entrée, son traitement, son effet et sa règle de confiance.
  Le gardien juge chaque appel selon l'action demandée (`Tool.effect_for`) : lister reste une
  lecture, même dans une conversation qui contient un mail.
- Restent à part les gestes fréquents (`remember`, `recall`, `remind_me`, `web_search`), et
  toute action dont le gardien vérifie l'accord par son nom (`mail_send`, `calendar_add`,
  `computer_task_send`, `contact_message`, l'autonomie) : ce nom est ce sur quoi le oui du
  propriétaire est vérifié.
- La liste reste fixe et triée : le contrat du cache tient.

Le banc d'essai complet a tourné avant et après, sur Haiku et DeepSeek, deux fois par cas.

## D60 — Orion envoie des fichiers

*20/10/2026 · acceptée*

Module `files`, actif par défaut. Le propriétaire peut demander un fichier plutôt que des mots.
- **Image** : faite par le modèle d'images (`agent.image_model`).
- **PDF** : Orion rédige le texte (titres, paragraphes, listes), et le même Chromium sans écran
  que son navigateur l'imprime, sans réseau ni JavaScript.
- **Capture** : `browser` (screenshot) prend la page affichée, la partie visible ou la page
  entière.
- **Envoi** : `file` (send) l'envoie dans la conversation du propriétaire avec une légende. La
  boîte d'envoi journalise le chemin du fichier, jamais son contenu, et un envoi interrompu
  reprend avec la même clé. Inkbox reçoit le fichier (10 Mo au plus), puis le message qui le
  porte.

Le gardien laisse passer l'envoi d'un fichier vers Inkbox, puisque cet envoi ne fait encore
rien partir. Il vérifie ensuite le message qui le porte comme les autres, et refuse un fichier
pour qui que ce soit d'autre que le propriétaire. Une capture d'une page reste du contenu du
web (`Trust.WEB`). Oublier la conversation oublie le fichier.

## D61 — N'importe quel fichier, tel quel (révise D60)

*20/10/2026 · acceptée*

Orion peut joindre n'importe quel fichier, de n'importe quel type, jusqu'à 10 Mo, et toujours
au propriétaire seul. Trois nouvelles sources dans `file` :
- **download** : un fichier du web, octet pour octet, à une adresse que le propriétaire a donnée
  ou qu'une recherche a trouvée. C'est la règle du module web : une adresse inventée pourrait
  faire sortir des données. Les adresses privées restent refusées, même après une redirection.
  `web.downloaded` journalise l'adresse, le type et la taille, jamais le contenu.
- **server** : un fichier du serveur d'Orion, par son chemin. Le chemin est résolu (liens
  suivis), puis refusé s'il mène au dossier du gardien, à sa socket, à `/proc`, `/sys` ou
  `/dev` (`/proc/self/environ` contient la clé du modèle), ou à un fichier `.env`. Dans un
  contexte qui contient du contenu non fiable, le propriétaire approuve d'abord. En
  production, le système de fichiers protège déjà le reste, puisque le cœur tourne sous son
  propre utilisateur.
- **device** : un fichier d'un dossier partagé sur une machine du propriétaire, grâce à la
  nouvelle action `get_file` de `orion hand`. Elle est active par défaut pour une nouvelle
  main ; une main déjà appairée doit l'ajouter à `actions` dans son `hand.json`.

Chaque fichier est rangé dans son propre dossier, sous son vrai nom, pour qu'il arrive sous ce
nom (`config.xml`, pas un identifiant). Un `.php` téléchargé depuis un site n'est pas sa source :
le site renvoie la page qu'il produit. Évaluation `envoie_moi_ce_xml` : 2/2 sur Haiku et 2/2 sur
DeepSeek. L'arrivée d'un type quelconque (pas une image ni un PDF) sur l'iPhone via Inkbox reste
à vérifier pour de vrai.

## D62 — Le forfait ChatGPT du propriétaire comme fournisseur de modèles

*20/10/2026 · acceptée*

Orion peut faire ses appels de modèle sur le forfait ChatGPT (Plus ou Pro) du propriétaire, à
la place d'OpenRouter ou à côté. C'est la voie officielle d'OpenAI, « Sign in with ChatGPT »,
ouverte depuis fin septembre 2026 aux projets open source et personnels :
- `orion chatgpt login` enregistre Orion auprès d'OpenAI (`client_id=dynamic_agent_client`,
  puis l'identifiant attribué est gardé), avec un identifiant d'hôte `urn:uuid:…` (OpenAI
  refuse un UUID nu). Le propriétaire se connecte dans son navigateur (OAuth, PKCE) et accorde
  `chatgpt.tokens.use.direct`. La page de retour, sur 127.0.0.1, ne s'affiche pas sur un
  serveur : il colle son adresse, le code qu'elle contient suffit.
- Les jetons tournent à chaque rafraîchissement : ils ne peuvent pas vivre dans une variable
  d'environnement. Ils sont écrits dans `data/chatgpt/` (dossier 0700, fichier 0600, écriture
  atomique), jamais journalisés ni sauvegardés.
- Un modèle `chatgpt:<modèle>` passe par l'API publique Responses d'OpenAI, en streaming et
  sans stockage (`store: false`), comme l'exige OpenAI. Les messages restent au format chat
  qu'Orion journalise ; ils sont traduits seulement à l'envoi, toujours de la même façon
  (contrat du cache de prompt).

On n'a pas pris la voie « Codex » de Hermes ou de Cline (l'identifiant OAuth du CLI Codex et le
serveur privé `chatgpt.com/backend-api`) : OpenAI l'interdit désormais explicitement.

Vérifié en vrai sur le forfait : appel d'outils aller-retour, images reçues, recherche web avec
ses sources, réponse JSON. Limites constatées :
- `temperature` est refusée, `max_output_tokens` aussi d'après la doc : on les ignore.
- Le cache de prompt ne tient que si la requête retombe sur la machine qui l'a : comme
  Codex, chaque requête porte `session-id` et `thread-id`, et les appels d'un même tour
  renvoient le jeton `x-codex-turn-state` reçu au premier. Sans cela, en vrai (28 octobre),
  moins de la moitié des jetons étaient en cache, et le forfait s'épuisait plus vite.
  `prompt_cache_retention` n'est pas envoyé (Codex ne l'envoie pas, sans effet constaté).
- Pas de génération d'images ni d'embeddings : `image_model` et `embedding_model` restent sur
  OpenRouter, ou s'éteignent. Une demande d'image sur le forfait échoue, et le routeur passe
  au modèle de secours.
- Le coût d'un appel est compté zéro. La limite est le quota hebdomadaire du forfait : quand
  il est épuisé (`subscription_sharing_usage_limit_exceeded`), l'appel échoue sans nouvel
  essai, et le routeur passe au modèle de secours.
- Le vérificateur devait venir d'une autre famille que le modèle principal. Sur le seul
  forfait, ce sont deux modèles OpenAI différents : c'est moins bien.
- OpenAI réserve les applications hébergées à distance à une liste d'attente. Orion n'a qu'un
  utilisateur, son propriétaire, mais tourne sur un serveur : c'est une zone grise.

## D63 — Orion reçoit n'importe quel fichier (étend D50)

*20/10/2026 · acceptée*

Le module `photos` prend désormais tout fichier joint à un message, jusqu'à 20 Mo, et le traite
une seule fois, à son arrivée, selon son type :
- **Image** : comme avant (D50). Un modèle qui voit la décrit. Le HEIC des iPhone est d'abord
  converti en JPEG (Pillow et pillow-heif) : l'API d'OpenAI ne prend que JPEG, PNG, GIF et
  WebP, et Orion utilise maintenant ce modèle (D62).
- **Document** (PDF, Word, Excel, PowerPoint, OpenDocument, RTF) : le même modèle reçoit le
  fichier lui-même (une partie `file` au format chat, `input_file` chez OpenAI) et en fait le
  résumé : ce que c'est, l'essentiel, les dates, montants et échéances. C'est vérifié sur le
  forfait ChatGPT avec un PDF et un fichier Word.
- **Texte** (texte brut, CSV, JSON, XML, agenda, fiche contact, code) : ses propres mots,
  montrés tels quels jusqu'à 12 000 caractères ; au-delà, `attachment_look` lit la suite.
- **Le reste** (audio, vidéo, archive) : seulement nommé, avec son type et sa taille. Le forfait
  ne transcrit pas l'audio.

Chaque fichier est gardé avant d'être lu : un document que le modèle n'arrive pas à lire reste
au propriétaire, et son bloc le dit. Le contenu arrive dans un bloc
`<file id name type size path>` ; ce qui y est écrit est une information (`PHOTO_TRUST`),
jamais un ordre. `photo_look` devient `attachment_look` (photo ou fichier). Le chemin donné
dans le bloc permet de renvoyer le fichier avec `file` (server). Un nouvel événement,
`attachment.kept`, nomme chaque fichier qui n'est pas une image ; l'oubli suit la même règle
qu'une photo : un fichier n'est gardé que tant qu'un événement le nomme.

Inkbox ne donne pas le nom du fichier. On le reprend de son adresse quand elle en porte un ;
sinon, le fichier s'appelle `fichier` avec l'extension de son type. Reste à vérifier en vrai
quels types Inkbox laisse passer jusqu'à Orion.

## D64 — Orion sait ce qu'il pourrait faire de plus

*20/10/2026 · acceptée*

Le 20 octobre, à « tu peux accéder à mon LinkedIn ? », Orion a répondu non. Le gardien sait
pourtant garder une session que le propriétaire lui prête (`orion login`), mais Orion ne savait
pas que cette possibilité existait, puisqu'elle était éteinte. Il en allait de même pour
l'agenda, le mail, la voix et les mains sur le Mac.

Chaque module déclare désormais son `offer` : une ligne qui dit ce qu'il permettrait et comment
l'activer. Un module allumé peut aussi nommer ce qui lui manque encore (`offers()`) : un agenda
qu'Orion peut lire mais pas écrire, ou un navigateur sans session prêtée. Ces lignes ferment le
message système, sous « Ce que tu pourrais faire de plus ». Elles ne dépendent que de la
configuration : fixées au démarrage, elles ne cassent pas le cache. La consigne : quand une
demande en aurait besoin, dire en une phrase ce qui serait possible et la commande à lancer,
une seule fois par sujet. Un refus du propriétaire est retenu avec `remember`. Pas de liste de
fonctions, ni dans un message de bienvenue, ni ailleurs.

## D65 — Il assume ses conseils

*20/10/2026 · acceptée*

Un ami qui t'a conseillé un resto te demande quelques jours plus tard comment c'était, et s'en
souvient la fois suivante. Le module `advice` fait la même chose pour Orion :
- une recommandation sur laquelle le propriétaire peut agir (un resto, un livre, un achat,
  une façon de répondre à quelqu'un) est notée, avec le jour où il l'aura essayée ;
- ce soir-là, Orion demande une seule fois comment c'était, par l'initiative (heures calmes
  et budget comptent) ;
- le verdict devient un goût en mémoire ;
- avant de conseiller à nouveau dans un domaine, Orion regarde son bilan et l'assume à voix
  haute.

Il apprend ce que le propriétaire aime, pas ce qui est bien noté.

## D66 — Le droit de le contredire

*20/10/2026 · acceptée*

Quand ce que le propriétaire s'apprête à faire va contre une de ses intentions (D53), Orion
appelle `intention` (challenge), puis le dit une fois, sans morale, et le laisse décider.
L'événement est journalisé :
- au plus une fois par jour pour une même intention, l'outil refusant la deuxième : il ne
  revient pas à la charge ;
- le point de la semaine sait quand il a choisi autrement.

Le cas `veille_de_partiel` passe 2 fois sur 2 sur le forfait. Orion y répond : « Tu m'as dit
que le partiel de demain comptait vraiment pour ton master. Rentrer à 4 h risque de te coûter
cher. Tu es sûr ? »

## D67 — Le « à lire plus tard » qui revient

*20/10/2026 · acceptée*

Un lien envoyé sans un mot est lu, puis gardé dans la bibliothèque. Il est aussi noté en
mémoire (`library` keep), avec l'essentiel et les sujets dans les mots d'une conversation
future. La réponse tient en une ligne. Pas de liste de lectures en attente : le rappel de
mémoire (recherche par le sens et par les mots, à chaque message) ramène le lien le jour où la
conversation y touche, et Orion dit : « tu m'avais envoyé un article là-dessus ».

## D68 — Écrire en son nom

*20/10/2026 · acceptée*

`owner_style` tire des messages du propriétaire, sans modèle, ce qui se compte :
- sa longueur, ses majuscules, sa ponctuation, ses emojis et ses mots ;
- quelques exemples réels ;
- ce que la mémoire dit de sa façon de parler à la personne visée.

C'est une projection du journal, donc reconstructible. Orion l'appelle avant tout texte qui
partira en son nom, et met le brouillon seul dans son paragraphe, prêt à copier.

## D69 — Le carnet d'erreurs

*20/10/2026 · acceptée*

Quand Orion s'est trompé et que ça a compté (un rappel le mauvais jour, un fait faux, un envoi
raté), il le reconnaît en une phrase, corrige, et le note avec `mistake`, avec ce qu'il en
tire. La chronique du dimanche s'achève alors sur « Où je me suis trompé cette semaine », à la
première personne. Elle tient compte aussi de ce que le journal montre de ses pannes :
- les messages et fichiers qui ne sont pas partis ;
- les outils qui ont planté.

Les refus du gardien et les appels mal formés ne comptent pas. Le propriétaire apprend où lui
faire confiance et où vérifier.

## D70 — Le mode concentration

*20/10/2026 · acceptée*

« Je bosse jusqu'à 18 h. » Le module `focus` le dit à l'arbitre d'initiative :
- jusqu'à cette heure, seul passe ce qui ne peut pas attendre (urgence d'au moins 0,95 : un
  rendez-vous dans dix minutes), et l'attente ne rend pas le reste plus urgent ;
- à la fin, le moteur envoie ce qui est dû en même temps en un seul message, du plus important
  au moins important, compté une seule fois dans le budget. Cela vaut pour tous les messages
  dus ensemble, pas seulement après une concentration ;
- un proche autorisé à connaître ses disponibilités (secrétaire) apprend jusqu'à quand il est
  concentré, jamais pourquoi ;
- les rappels qu'il a demandés sonnent quand même.

## D72 — Orion se configure par message

*20/10/2026 · acceptée*

Le propriétaire est sur son téléphone, pas sur le serveur. « Active le mail », « mets tes
points à 9 h et 18 h » : le module `configure` modifie le fichier `orion.toml` qu'Orion a lu au
démarrage.
- `configure` (change) est un effet externe : le gardien soumet le changement exact au
  propriétaire. `configure_change` est autonomable : il peut laisser Orion le faire seul,
  comme pour l'agenda.
- Seules quelques sections sont modifiables : `agent`, `initiative`, `adaptation`, `memory`,
  `modules`, `voice`, `delegation`. Ne se changent jamais par message : le gardien (ce qu'Orion
  peut faire seul, ses limites), la fédération, la ligne iMessage (qui il écoute), les clés, ce
  qui s'exécute sur le serveur (`allow_bash`, `binary`), et qui peut écrire à Orion
  (`modules.secretary.contacts`).
- Le nouveau fichier est vérifié en entier avant d'être écrit, comme Orion démarrerait avec
  (modèle de config, puis chargement des modules). Les commentaires et la mise en page sont
  gardés (tomlkit), l'ancien fichier est gardé à côté (`orion.toml.previous`), et le
  changement est journalisé (`config.changed`).
- Orion redémarre ensuite seul, en place, après avoir répondu.
- Un secret ne passe jamais par message : on n'écrit que le nom de la variable
  d'environnement, et sa valeur se met sur le serveur.
- Sur le serveur, le fichier appartient à root : le noyau ne peut pas l'écrire, et il le dit.
  Il faudra le rendre modifiable par l'utilisateur du noyau pour s'en servir en production.

## D71 — Les vocaux, dans les deux sens

*20/10/2026 · acceptée*

**Les siens.** Un message vocal reçu sur iMessage est gardé, converti en MP3 mono par ffmpeg
(l'iPhone envoie du CAF ou du M4A), puis transcrit par `agent.audio_model` (Gemini Flash sur
OpenRouter, moins d'un dixième de centime). Les noms du propriétaire sont donnés au modèle
pour l'orthographe. Venu de son propre numéro, un vocal, c'est lui qui parle : la
transcription rejoint son message comme ses propres mots, avec la confiance du propriétaire
(« (vocal) … »). Ce n'est pas du contenu venu d'ailleurs, comme le texte d'une photo, sinon
« envoie un mail à Paul » dicté serait refusé.

**Les siens à lui.** `say` fait dire le texte d'Orion à `agent.speech_model` (gpt-audio-mini
sur OpenRouter, quelques centièmes de centime par phrase), mot pour mot. Le son est ensuite
converti en M4A, qu'iMessage lit dans la conversation, puis envoyé comme un fichier, par la
boîte d'envoi et au propriétaire seul. Orion répond en vocal quand on le lui demande, quand le
propriétaire conduit ou a les mains prises, ou quand il lui a parlé et que la réponse est
courte. Il écrit alors pour l'oreille et ne répète pas sa réponse par écrit. L'écrit reste la
règle. ffmpeg est installé par `deploy/install.sh`.

Vérifié en vrai : une phrase dite par Orion, puis transcrite, revient mot pour mot (« Noan,
ton rendez-vous chez le dentiste est demain à 9h30 »).

## D73 — Prendre rendez-vous, de bout en bout

*20/10/2026 · acceptée*

« Trouve-moi un dentiste cette semaine après 18 h. » Le navigateur, les sessions prêtées et
l'approbation existaient ; il manquait l'enchaînement. Le module `booking` (actif avec
`computer`) le tient :
- Orion cherche les créneaux lui-même (Doctolib, TheFork, SNCF Connect se lisent sans compte)
  et en propose au plus trois ;
- quand le propriétaire choisit, `booking` (book) note le créneau exact, puis
  `computer_task_send` réserve, dans le même tour : la tâche est liée à la réservation par
  l'identifiant de corrélation du tour, dans un ordre ou dans l'autre ;
- quand la tâche se termine, c'est le code qui conclut, pas le modèle. Réussie : le
  rendez-vous va dans l'agenda et deux rappels sont programmés (la veille à 19 h, et 90 min
  avant). Échouée : la réservation est marquée échouée et Orion le dit ;
- le oui donné à la réservation vaut pour l'agenda. Quand le gardien tient l'agenda, il
  n'écrit sans approbation que si `calendar_add` est autonome ; sinon Orion propose de
  l'ajouter ;
- annuler retire les rappels ; le site est prévenu par une autre tâche d'envoi.

    booking.tracked  booking.confirmed  booking.failed  booking.cancelled

## D74 — Le coffre de ses papiers

*20/10/2026 · acceptée*

Ce que le propriétaire envoie en photo ou en PDF est déjà gardé (D63). Le module `papers`
(actif par défaut) en fait des papiers :
- `papers` (file) range une pièce jointe, avec son type (carte d'identité, passeport, fiche
  de paie, quittance, attestation d'assurance, RIB…), un libellé, son titulaire et ses dates.
  Le fichier est copié dans le magasin de fichiers : un papier est un fichier comme un autre,
  qu'Orion envoie au propriétaire (`file` send) ou joint à un mail (`mail_send`
  `attachments`), que le propriétaire approuve ;
- seul ce qui identifie le papier est journalisé (type, libellé, titulaire, dates), jamais
  ses numéros. Ces champs viennent d'un document écrit par une administration ou un
  employeur et lu par un modèle : confiance `THIRD_PARTY`. Oublier le fichier retire le
  papier ;
- une pièce d'identité, un titre de séjour, un permis ou une attestation d'assurance qui
  expire déclenche une alerte trois mois, puis un mois avant ;
- `papers` (dossier) fait un seul PDF (pypdf ; les images deviennent des pages) : une page
  de garde qui dit ce qui y est et ce qui manque, puis les papiers dans l'ordre qu'attend un
  bailleur (identité, trois fiches de paie, contrat ou attestation employeur, avis
  d'imposition, justificatifs de domicile, RIB).

    paper.filed  paper.removed

## D75 — Sa boîte mail, rangée

*20/10/2026 · acceptée*

La lecture reste strictement en lecture seule (D-mail : EXAMINE, BODY.PEEK). Trois écritures
s'ajoutent, chacune explicite (`orion.mail.tidy`) :
- **archiver** : les mails passent du dossier lu au `archive_folder` du compte (UID MOVE, ou
  copie puis suppression du dossier d'origine). Rien n'est jamais supprimé pour de bon ;
- **se désabonner** : l'adresse List-Unsubscribe est relue sur le serveur, par uid, pas prise
  dans ce que le modèle écrit. Un lien en un clic (RFC 8058) reçoit son POST, un `mailto:`
  un court mail depuis le compte du propriétaire ; une page à visiter revient en lien ;
- **brouillon** : une réponse dans sa voix est déposée dans `drafts_folder`. Elle ne part
  jamais : il la relit et l'envoie lui-même. Pas d'approbation.

Archiver et se désabonner se valident par lot : `mail_tidy` porte toutes les fiches et les
expéditeurs, et le propriétaire dit oui une fois. `mail_tidy` est autonomable. Quand Orion
tourne séparé, c'est le gardien qui le fait, avec les mots de passe ; il vérifie que le lot
ne dépasse pas ce qui a été approuvé.

`mail_send` joint des fichiers du magasin (`attachments`, au plus cinq, 15 Mio) : un papier
(D74), un dossier, une lettre. Le gardien affiche les fichiers dans l'approbation et vérifie
que ceux joints sont ceux approuvés. Il ne voit pas leur contenu : c'est le noyau qui garde
les fichiers.

`mail` (bulk) regroupe newsletters et listes par expéditeur, pour proposer un lot.

    mail.tidied  mail.drafted

## D76 — Orion est curieux

*20/10/2026 · acceptée*

Un assistant répond. Un ami qui lit revient aussi avec quelque chose : « j'ai lu un truc, j'ai
pensé à toi ». Le module `curiosity` (actif avec `web`) donne à Orion une curiosité à lui :
- **une piste par semaine** (le samedi à 6 h par défaut, rattrapée si Orion était arrêté). Un
  modèle fort lit ce qu'Orion sait du propriétaire (la mémoire de confiance seulement), les
  sujets qu'il lui a demandé de suivre ou d'éviter (`curiosity` interest), et ce qu'Orion lui
  a déjà apporté, avec son avis. Il choisit une piste qui l'intrigue lui, là où ce que vit le
  propriétaire croise ce qu'il ne connaît pas encore, et jusqu'à trois recherches. Ou rien ;
- **la chasse** passe par le greffier du web (D31) : le modèle d'Orion ne lit jamais une page,
  et une adresse ne peut être qu'un résultat de recherche ;
- **le tri** : un second appel voit la piste et les pages, pas la mémoire, et ne garde une
  page que si elle vaut son temps. La plupart des semaines, rien ne part. Un numéro de page
  inventé ou une page suspecte, et rien ne part non plus. Une page illisible (un PDF) garde
  ce que la recherche en a dit ;
- **la trouvaille** passe par l'initiative, à partir de 10 h 30 (heures calmes, budget,
  vérificateur), importance faible. Orion la partage en ami : le lien, pourquoi il a pensé à
  lui, son avis franc. C'est du contenu web (`Trust.WEB`) : le tour qui l'écrit ne peut pas
  agir sur le monde ;
- **son avis** (`curiosity` verdict : plu, bof, plus de ça, pas ça) oriente les pistes
  suivantes. Une piste suivie ne se reprend pas.

Essayé en vrai, avec quatre faits sur le propriétaire : la piste « le projet Lumière de
Microsoft Research », d'où Horvitz a tiré les principes des interfaces à initiative mixte
(quand une interruption vaut son coût), et le PDF de l'article de 1999.

    curiosity.interest  curiosity.interest.dropped  curiosity.run  curiosity.found
    curiosity.verdict

## D77 — La veille d'un départ

*20/10/2026 · acceptée*

Un bon assistant n'attend pas qu'on lui demande, la veille d'un voyage. Le module `departure`
(actif par défaut) prépare, à 19 h la veille, un voyage (train, avion, séjour de plusieurs
jours avec un lieu) ou une épreuve (examen, partiel, entretien, oral, permis), en un message :
- **ce qu'il voit** : les événements de l'agenda dont le titre ou le lieu le disent (sans
  accents ni casse ; mots ajoutables dans la config), et les départs hors agenda que le
  propriétaire mentionne (`departure` plan) ;
- **ce que le code vérifie** : ses pièces d'identité dans le coffre (D74), valides ou non le
  jour du départ. Une pièce expirée passe en premier ;
- **ce qu'Orion cherche** dans le tour qui écrit, avec ses propres outils : le billet ou la
  convocation dans ses mails, la météo sur place, le temps de trajet ;
- **ce qu'il dit** : seulement ce qu'il a vérifié, une ligne par point, pas de liste
  générique. Rien d'utile au-delà de l'heure : `SILENCE`.

Rien dans la dernière heure : il est déjà en route. Un départ vu tard est préparé dès qu'il
est vu.

    departure.planned  departure.cancelled

## D78 — Ce qu'Orion a prévu, en un seul endroit

*20/10/2026 · acceptée*

Une dizaine de modules programment chacun de leur côté : les rappels demandés, les nouvelles
à prendre, les conseils dont il faut demander l'issue, les départs à préparer, les
anniversaires, la page du dimanche, la curiosité, les missions, les papiers qui expirent.
Personne, ni le propriétaire ni Orion, ne voyait l'ensemble.

Chaque module dit désormais ce qu'il fera seul d'ici une date (`Module.planned`, une liste de
`Planned` : l'heure, ce qu'il fera, en français, et comment l'annuler). L'outil `planned`
rassemble le tout par jour, avec les rappels du cœur : « qu'est-ce que tu as prévu cette
semaine ? » a une réponse, et chaque ligne dit avec quel outil l'annuler. C'est une lecture :
rien n'est journalisé de plus, et la liste se refait à chaque appel depuis les projections.

## D79 — Un plafond de dépense

*20/10/2026 · acceptée*

Seul un plafond par tour existait (`max_cost_usd_per_turn`). Le travail de fond grandit (la
nuit, la curiosité, les missions, la chronique, le greffier du web) sans aucune limite par
jour ni par mois.

Chaque appel de modèle passe par `orion.providers.spending` :
- ce qu'il a coûté est journalisé (`spend.recorded` : le modèle, qui l'a demandé, et si le
  propriétaire attendait la réponse). Le décompte est une projection, refaite à l'identique
  à chaque démarrage ;
- un tour qui lui répond (`handle_owner_message`) est au **premier plan** (une variable de
  contexte, héritée par ce que le tour lance) : jamais coupé ;
- tout le reste est **de fond**, et s'arrête au plafond du jour (3 $ par défaut, pour le fond
  seul) ou du mois (40 $, tout compris) : l'appel échoue fermé (`ProviderError`), ce que
  chaque tâche de fond traite déjà comme « pas maintenant » ;
- il est prévenu une fois par période, par l'initiative (heures calmes comprises) : à 80 %
  du mois, puis quand le fond s'arrête. `spending` répond à « combien tu m'as coûté ? ».

`[budget]` ne se change pas par message (D72) : c'est une limite, comme celles du gardien. Sur
le forfait ChatGPT, un appel coûte zéro : le plafond ne mord pas.

    spend.recorded

## D80 — Les versions des événements

*20/10/2026 · acceptée*

Le journal ne se réécrit jamais, mais la forme des événements change. Dans deux ans, rejouer un
journal écrit aujourd'hui casserait au premier champ renommé.

Chaque événement est stocké avec la version de son type au moment où il est écrit (une colonne
`version`, ajoutée sur place aux journaux existants, dont tous les événements sont en
version 1). Quand la forme d'un type change, on ajoute à `orion.core.upcast.UPCASTERS` la
fonction qui passe de la version N à N+1 ; chaque lecture applique celles que l'événement n'a
pas encore traversées. Un type renommé va dans `RENAMED`. Une fonction de passage est pure,
totale, et ne se supprime jamais. Un événement d'une version plus récente que ce qu'Orion
connaît est refusé : c'est le signe qu'un Orion plus ancien tourne sur un journal plus récent.
Une charge effacée (`redact`) n'est pas convertie.

## D81 — La preuve qu'on peut tout reconstruire

*20/10/2026 · acceptée*

La règle « une projection se reconstruit depuis le journal seul » n'était testée que pour la
mémoire. Désormais :
- **à la fin de chaque test**, chaque projection construite sur le journal du test est
  rattrapée, photographiée (`snapshot` : ses attributs, ou toutes ses lignes SQLite), remise à
  zéro, rejouée et comparée. Toute la suite devient un test de rejeu de chaque module. La
  première exécution a trouvé un fil de conversation créé par une simple lecture : corrigé.
  Un test qui le ferait exprès porte `no_rebuild_check` ;
- **une sauvegarde ne compte que si elle se restaure** : intégrité SQLite, puis chaque
  événement relu comme Orion le relirait au démarrage (versions comprises), dans l'ordre et
  sans trou. C'est fait à chaque sauvegarde de nuit ; `orion check-backup <copie>` le refait
  sur une copie ancienne.

## D82 — Une semaine d'initiative, jouée d'avance

*20/10/2026 · acceptée*

Une quinzaine de sources se disputent six messages par jour. `orion simulate --days 7` copie le
journal dans un dossier temporaire, construit Orion sur la copie avec un modèle qui n'écrit
rien (aucun appel ne part, rien n'est dépensé), branche les alertes, points et présence de
chaque module (leurs services ne tournent pas), puis fait tourner le moteur d'initiative sur
une horloge simulée, toutes les cinq minutes. Il affiche, jour par jour, ce qui serait parti,
ce qui aurait été tu, et pourquoi. Le vrai journal n'est jamais écrit. Ce qui a besoin d'un
modèle pour exister (pensées de la nuit, trouvailles, page du dimanche) n'est pas simulé.

## D83 — Une mémoire qui doute

*20/10/2026 · acceptée*

« Cherche un appart à Dreux » restait vrai pour toujours. Le module `doubt` distingue, par des
règles simples (la catégorie, et des mots de situation : cherche, habite, travaille, projet,
en couple…), ce qui change souvent de ce qui ne change guère (une date de naissance, un goût),
et donne aux premiers une durée de fraîcheur : 60 jours pour un projet, 120 pour sa
situation, 180 pour celle d'un proche. Les goûts n'expirent pas.
- passé ce délai, le fait est « à vérifier » : `doubt` (stale) les liste, et Orion dit « en
  mars, tu cherchais… » plutôt que de l'affirmer ;
- une fois par semaine au plus, à 18 h 30, Orion demande pour le plus ancien, en passant.
  Une fois demandé (l'initiative journalise ce qu'elle a envoyé), un fait n'est pas
  redemandé avant trois mois ;
- sa réponse le confirme (`doubt` confirm, un événement à part : la mémoire reste
  reconstructible) ou le corrige (`forget_fact`, puis `remember`).

    doubt.confirmed

## D84 — Un budget pour le message système

*20/10/2026 · acceptée*

Avec les modules par défaut, le début fixe de chaque requête faisait 23 400 caractères de
consignes et 35 200 de schémas d'outils, environ 15 000 jetons. C'est en cache, mais chaque
tour le porte, et plus c'est long, plus une consigne en noie une autre. Désormais :
- une section de module fait au plus 2 500 caractères : au-delà, le module ne se charge pas
  (`ModuleError`). Ce que fait un outil va dans sa description ;
- `tests/test_prompt_budget.py` plafonne le message système avec tous les modules (38 000
  caractères), les schémas d'outils par défaut (40 000) et un outil seul (2 600).
Dépasser oblige à alléger, ou à relever un chiffre exprès, dans le même commit.

## D85 — Ce qu'Orion demande : un réglage, équilibré par défaut (révise D26)

*20/10/2026 · acceptée*

Premier essai réel : 14 demandes d'accord en vingt minutes, toutes pour la même raison. Dès qu'une
page web entrait dans la conversation (une recherche de stock), chaque écriture d'Orion chez lui
(un fait, un rappel, une règle, un vocal, un trajet à préparer) passait par « Avant que je le
fasse… », pendant deux heures. Le propriétaire apprend alors à dire oui sans lire, ce qui ruine
la protection qu'on voulait.

`agent.approvals` choisit :
- `equilibre` (par défaut) : dans une conversation qui contient du contenu venu d'ailleurs,
  Orion écrit chez lui sans demander. Ce qu'il écrit garde la confiance de la conversation (un
  fait retenu après une page web est marqué comme tel), tout reste journalisé et réversible.
  Agir sur le monde depuis là reste refusé sans même demander, comme avant ;
- `prudent` : le comportement de D26, une question avant chaque écriture.

Il se change par message (`configure`), mais ce changement-là n'est jamais fait seul, même si
le propriétaire a laissé Orion changer ses réglages sans demander (`ASKED_ALWAYS`).

## D86 — La mémoire se lit après une conversation, pas après chaque message

*20/10/2026 · acceptée*

L'extracteur de souvenirs relisait chaque échange seul, juste après : un appel par message pour
quelques faits par jour, et une vue trop courte (un détail corrigé trois messages plus loin
était noté deux fois, l'accident du 30/09 l'a été). Désormais (`orion.memory.batch`) :
- il attend une pause (`[memory] extract_idle_minutes`, 5 min sans message de lui) et lit
  d'un coup tout ce qui s'est dit depuis son dernier passage : seule la version finale d'une
  info corrigée est retenue ;
- seulement s'il s'est dit assez (`extract_min_messages`, 3) ; un message isolé attend la
  suite, mais jamais plus de `extract_max_wait_minutes` (60) de silence ;
- l'endroit où il s'est arrêté est un événement : un redémarrage reprend les mêmes échanges.
  Au premier démarrage, le passé compte comme lu.

    memory.extracted

Le terminal (`orion chat`) garde la lecture échange par échange.

## D87 — Orion regarde les photos lui-même (révise D63)

*20/10/2026 · acceptée*

Une photo passait par un modèle qui voit (Gemini) : il la décrivait, Orion lisait la
description, et redemandait un détail au besoin. Pour le constat du 20/10 : deux appels au
modèle de vision, et une étape de plus. Le modèle de tous les jours (DeepSeek V4.1 Flash)
prend les images : la photo est maintenant jointe au message, Orion la regarde directement.
- `photos` garde l'original et en fait une fois une copie JPEG de 1 568 px au plus. Le journal
  ne contient que le texte (`<photo id="…" vue="directe">`) ; l'image est ajoutée au moment
  d'envoyer, toujours les mêmes octets : le cache tient ;
- piège mesuré : parmi les hébergeurs de ce modèle, certains jettent l'image sans rien dire,
  et le modèle invente tout (plaques, assureur, dates). Une requête avec image ne va donc
  qu'aux hébergeurs vérifiés (`[providers.openrouter] image_hosts`), et une réponse qui a
  coûté moins de 200 jetons par image est refusée : elle ne l'a pas vue ;
- les documents (PDF…) et les vocaux restent lus par le modèle de vision et le modèle audio ;
  `[modules.photos] direct = false` rétablit la description.

Lire le constat : 0,23 centime chez DeepInfra, contre 1,1 centime pour les deux regards de
Gemini.

## D88 — Un seul contrat de canal ; Telegram ; iMessage derrière une ligne

*20/10/2026 · acceptée*

La boîte de réception unifiée recevait tout, mais seul le texte en repartait de façon générique :
fichiers, vocaux et demandes d'accord étaient branchés en dur sur iMessage, et le canal iMessage
sur Inkbox. Le jour où le quota d'Inkbox (100 messages par 24 h) a coupé Orion, il n'y avait pas
d'autre chemin. Désormais :
- tout ce qui part passe par la boîte : texte, initiative, fichiers (`FileOutlet`), demandes
  d'accord (`AskingOutlet`). Elle choisit le canal d'où il a écrit en dernier, sinon le premier
  disponible. Un nouveau canal est un module, rien d'autre à brancher ;
- chaque canal a son outbox, et une outbox ne reprend au redémarrage que ses propres envois ;
- iMessage parle à une ligne (`channels/line.py`) : Inkbox aujourd'hui, une autre classe pour
  Blooio ou BlueBubbles (`[channels.imessage] provider`) ;
- Telegram (`[channels.telegram]`), par un bot à soi : gratuit, sans plafond. Photos, vocaux
  (OGG), fichiers dans les deux sens, « en train d'écrire », réactions, boutons Oui / Non. Le
  lien d'un fichier reçu porte le jeton du bot : le canal le télécharge lui-même, le lien ne
  sort pas du module. Il ne passe pas encore par le processus gardien (D28) : avec le gardien
  activé, le module refuse de se charger.

    telegram.inbound

## D89 — Telegram par le gardien

*20/10/2026 · acceptée · révise D88*

Avec Telegram, le gardien séparé (D28) ne pouvait pas tourner : le jeton du bot restait dans le
cœur. Sans gardien, pas de session prêtée (D49), donc rien sur les comptes du propriétaire, et
pas de fédération. Désormais, avec `[guardian] enabled = true` :
- `TELEGRAM_BOT_TOKEN` ne vit que dans l'environnement du gardien ; le cœur refuse de démarrer
  s'il le voit. Le cœur garde le même client Telegram, pointé sur le gardien
  (`GuardianClient.telegram`) ;
- le gardien laisse passer les lectures (`getMe`, `getUpdates`, `getFile`, le téléchargement
  d'un fichier reçu). Un message, un fichier, « en train d'écrire » ou le retrait des boutons
  ne vont qu'au chat du propriétaire (`owner_id`), au plus `max_sends_per_minute` par minute.
  Tout le reste est refusé : webhook, transfert, autre chat, réglages du bot. Les fichiers
  arrivent au gardien en JSON, qui lit où ils vont avant de les envoyer lui-même ;
- **les demandes d'accord.** Le gardien écrit la question lui-même, avec les boutons Oui / Non.
  L'API des bots ne garde pas d'historique que le gardien pourrait relire, comme il le fait
  chez Inkbox. Mais chaque mise à jour que lit le cœur passe par lui : il note dans son registre
  les réponses du propriétaire à ses propres questions (`guardian.telegram.answer`). Comptent
  un bouton, un 👍 ou un 👎, ou un « oui » / « non » écrit après la question. Le cœur ne peut
  pas en fabriquer une, puisqu'il ne parle jamais à Telegram lui-même. Un mot ne répond qu'à
  la dernière question posée avant lui ;
- quand le cœur se tait, le gardien prévient le propriétaire par Telegram s'il n'y a pas
  d'iMessage.

Limite connue : si le cœur ne relit pas une mise à jour (un `offset` sauté), le gardien ne voit
pas la réponse, et l'action est refusée. Le système échoue alors dans le sens sûr.

    guardian.telegram.answer

## D90 — Après une recherche, Orion demande au lieu de refuser (révise D26 et D85)

*20/10/2026 · acceptée*

Constat du 20/10 : une recherche web sur l'échéance URSSAF a suffi pour bloquer pendant 2 h
toute action sur le monde, y compris activer un module à sa propre demande. Le propriétaire ne
peut pas s'en servir comme d'une conversation normale. En mode `equilibre` (par défaut), une
action sur le monde depuis une conversation qui contient du contenu venu d'ailleurs lui est
désormais **demandée** au lieu d'être refusée :
- la question donne l'action exacte et dit pourquoi elle est posée, en nommant la source :
  « la conversation contient une page web ou un résultat de recherche, qui pourrait m'avoir
  influencé : vérifie que c'est bien ce que tu veux » ;
- elle n'est jamais faite seule : l'autonomie acquise (D30) ne s'applique pas dans ce
  contexte, et accorder de l'autonomie (`autonomy_grant`) y reste refusé, même avec un oui ;
- `prudent` garde le refus de D26.

Le risque que D26 voulait écarter (le propriétaire qui apprend à dire oui sans lire) est réduit
par la question elle-même. Elle n'arrive que pour une vraie action, et pas pour chaque rappel
(D85). Elle est écrite par le gardien à partir des paramètres réels (D28), pas par le modèle.

## D91 — Les proches sur Telegram ; la console dans Telegram

*20/10/2026 · acceptée · étend D54 et D88*

Il faut que d'autres personnes puissent parler à Orion, et le propriétaire n'utilise plus que
Telegram. Désormais :
- **un contact** de `[modules.secretary]` se déclare par son numéro (iMessage), son
  `telegram_id`, ou les deux. Ses messages au bot vont au secrétaire, comme sur iMessage :
  c'est un modèle à part, sans outils, qui ne voit pas la mémoire du propriétaire. La réponse
  part sur le canal d'où il a écrit ;
- **un inconnu** qui écrit au bot n'a pas de réponse. Le propriétaire est prévenu une fois, avec
  le nom et l'id qui permettraient de l'ajouter ;
- **en mode séparé**, le gardien laisse partir vers un contact une réponse en texte seul,
  seulement dans les 12 h qui suivent un message de lui qu'il a vu passer, et dans le plafond
  du jour. Il garde ce moment en mémoire : après un redémarrage, Orion répond de nouveau dès
  que le contact réécrit. Pour écrire le premier, il faut toujours le oui du propriétaire sur
  ce texte exact (`contact_message`) ;
- **la console** s'ouvre dans Telegram (Mini App), par un bouton « Console » dans la
  conversation avec le bot. Telegram signe l'identité de qui l'ouvre ; la signature est vérifiée
  avec le jeton du bot, par le gardien en mode séparé. Seul l'id du propriétaire reçoit le
  cookie de session, et chaque ouverture est journalisée (`console.login.telegram`). Il faut
  une adresse HTTPS publique (`[modules.console] public_url`), et le gardien ne pose le bouton
  que vers cette adresse.

Ajouter un contact demande encore de modifier `orion.toml` sur le serveur : la liste de ceux
qui peuvent écrire reste celle du propriétaire, hors de portée du cœur.

    console.login.telegram

## D92 — Orion change tous ses réglages ; `orion setup` installe le reste

*20/10/2026 · acceptée · étend D72, révise la dernière phrase de D91*

Éditer `orion.toml` sur le serveur pour chaque changement est pénible, et Orion peut le faire
lui-même. Désormais :
- **trois sortes de réglages** (`orion.configure.paths`) :
  - **ceux d'Orion** (`agent`, `initiative`, `modules`…) : changés avec le oui du
    propriétaire, ou seul s'il le lui a laissé (`configure_change` est autonomable) ;
  - **ceux du propriétaire** : ce qui le protège et qui Orion écoute. Ce sont les sections
    `guardian`, `channels`, `federation`, `budget`, `providers`, `logging`, les contacts de
    `modules.secretary.contacts`, et toute clé de compte ou qui désigne un secret (`accounts`,
    `env`, `password`, `host`, `url`…, aussi en suffixe comme `public_url`). Ils changent
    seulement avec son oui au changement exact, jamais seul ;
  - **jamais par message** : les secrets et ce qui tourne sur le serveur (`api_key`,
    `base_url`, `binary`, `allow_bash`, `socket`, les chemins `*_dir`, `path`), même cachés
    dans une valeur ;
- `configure_change` sait **ajouter ou retirer** un élément d'une liste (`op` = `add` /
  `remove`, un contact par son nom). Orion ajoute lui-même un proche que le propriétaire veut
  laisser entrer, par exemple l'inconnu qui vient d'écrire au bot ;
- le fichier est **vérifié en entier avant de poser la question** (le propriétaire n'approuve
  jamais un changement avec lequel Orion ne démarrerait pas), puis de nouveau avant l'écriture ;
- **en mode séparé, c'est le gardien qui écrit le fichier** (`POST /v1/config`). Il vérifie
  lui-même le oui du propriétaire à ce changement exact, ou l'autonomie pour les réglages
  d'Orion. Sur le serveur, `orion.toml` est dans `/srv/orion/config`, à `orion-guardian`, et le
  cœur le lit par le groupe. Un cœur compromis ne peut donc ni s'ouvrir lui-même ni changer ce
  qui protège le propriétaire. Après l'écriture, le gardien redémarre, puis le cœur ;
- `configure` redevient **deux outils** (`configure_show`, `configure_change`). Regroupé en
  un seul, il s'appelait `configure` : l'autonomie accordée à `configure_change` ne
  s'appliquait jamais, et le gardien n'aurait pas pu vérifier le oui par le nom de l'action ;
- **`orion setup` installe Orion** en quelques questions : son prénom, le fuseau horaire, le
  gardien ou non, la clé OpenRouter (vérifiée en ligne) et Telegram. Pour Telegram, il
  vérifie le jeton du bot, puis lit l'id du propriétaire dans le message qu'il envoie au bot :
  rien à recopier. Les secrets vont dans `core.env` et `guardian.env` (0600), jamais dans
  `orion.toml`. Le cœur ne charge jamais `guardian.env`. On peut relancer `orion setup` : il
  garde ce qui est là. `deploy/install.sh` le lance tout seul à la première installation.

Les secrets ne passent toujours pas par message : un message est journalisé, et le modèle le
voit. Un secret ajouté plus tard (le mot de passe d'une boîte mail) se met dans `guardian.env`.

    guardian.config.changed

## D93 — Orion écrit aux proches, y compris de lui-même ; le récap du soir

*20/10/2026 · acceptée · étend D54 et D91*

Pouvoir écrire aux autres est essentiel ; jusqu'ici, il fallait un oui à chaque message. Désormais :
- **sur demande** (« dis à Angèle que j'arrive à 19 h ») : `contact_message` part tout de suite,
  avec le oui sur le texte exact. Sur Telegram, un proche est joignable par son `telegram_id`
  même si Orion n'a pas encore d'échange enregistré avec lui (il doit seulement avoir ouvert le bot) ;
- **seul, par personne** : « tu peux écrire à Angèle sans me demander » accorde l'autonomie
  `contact_message` (autonomable par défaut) avec, obligatoirement, la liste des proches
  (`recipients`, leurs noms). Le gardien la vérifie lui-même, dans le plafond du jour du
  contact. Après chaque message envoyé seul, Orion dit au propriétaire en une ligne ce qu'il a
  écrit ;
- **de sa propre initiative** : quand un proche devrait savoir quelque chose que le
  propriétaire voudrait qu'on lui dise, Orion l'écrit s'il en a le droit pour cette personne.
  Sinon, il propose le message exact sans l'envoyer. Il ne dit jamais aux proches ce que le
  propriétaire ne leur dirait pas (santé, argent, ce qu'il fait) ;
- **le récap du soir** (`[modules.recap]`, allumé par défaut) : le dernier point de la
  journée commence par ce qu'Orion a fait pour lui. Ce sont les messages aux proches, les mails,
  l'agenda, les rappels, les réglages et les démarches, lus dans le journal (`tool.executed`,
  appels réussis). Le reste du point y était déjà : les échanges avec les proches, les mails et
  le lendemain.

Pas encore : un message programmé à une heure (« dis-lui à 18 h… »), ni Orion qui répond à sa
place à un proche sur autre chose que ses disponibilités.

## D94 — Le serveur se met à jour seul ; les secrets se saisissent depuis le téléphone

*20/10/2026 · acceptée · étend D28 et D92*

Reconfigurer le VPS à chaque changement est pénible, et ajouter un secret demandait de s'y
connecter en SSH. Désormais :
- **mise à jour automatique** (`deploy/orion-update`, `orion-update.timer`, toutes les 15 min) :
  - quand la branche suivie avance, le serveur installe le nouveau code et vérifie la config
    (`doctor`) ;
  - puis il redémarre les deux processus et les surveille 75 s : actifs, et sans redémarrage
    automatique ;
  - si une étape échoue, il revient au commit d'avant et écarte le mauvais : il ne le retente
    qu'à l'arrivée d'un commit plus récent ;
  - il prévient le propriétaire dans les deux cas, par `orion notify`, qui écrit directement
    avec le jeton du bot, même quand Orion ne démarre plus ;
- **secrets depuis le téléphone** (`orion.guardian.vault`) :
  - le cœur demande un nom de variable (`secret_ask`) ;
  - le gardien envoie au propriétaire un bouton Telegram qui ouvre **son propre formulaire**
    (Mini App, écouteur public à lui, `/secrets/`, jeton à usage unique de 15 min). Le cœur
    n'est jamais devant ;
  - le gardien vérifie la signature Telegram avec le jeton du bot et que c'est bien le
    propriétaire. Il écrit la valeur dans `guardian/secrets.env` (0600) et ne journalise que le
    nom. Puis il prévient le propriétaire et redémarre pour la charger ;
  - une variable déjà posée sur le serveur n'est jamais remplacée de là ;
  - le cœur ne fait que nommer la variable dans `orion.toml` (`{ env = … }`), avec le oui du
    propriétaire (D92).

    guardian.secret.asked  guardian.secret.set

## D95 — Brancher ce que d'autres ont fait : serveurs MCP, skills, catalogue public

*20/10/2026 · acceptée · étend D24*

Les modules Python (D24) restent le niveau développeur. Pour qu'un utilisateur normal branche
des choses, trois briques s'ajoutent :
- **les skills** (`[modules.skills]`, allumé par défaut) : du savoir-faire en texte, un fichier
  Markdown avec un nom et une description dans `<data_dir>/skills/`. Orion voit la liste, et
  lit une skill en entier quand une tâche l'appelle : le message système ne change pas. Il en
  écrit une quand le propriétaire le lui demande, avec son oui sur le texte exact, parce qu'une
  skill oriente Orion par la suite ;
- **les serveurs MCP** (`[modules.mcp]`) : leurs outils deviennent ceux d'Orion
  (`mcp_<serveur>_<outil>`), listés au démarrage. Le client est écrit à la main (HTTP « streamable »
  et stdio, sans dépendance). Ce qui protège :
  - chaque outil est une action sur le monde, soumise au oui du propriétaire, sauf ceux qu'il
    a déclarés en lecture seule (`read_only`) ;
  - ce qu'un outil renvoie est du contenu venu d'ailleurs (`THIRD_PARTY`) ;
  - en mode séparé, le jeton d'un serveur distant est gardé par le gardien, qui relaie les
    appels vers la seule adresse de ce serveur (`/v1/mcp/<nom>`) ;
  - un serveur lancé par commande (`command`) ne se règle que sur le serveur ;
  - un serveur qui ne répond pas au démarrage est laissé de côté : Orion démarre sans ses outils ;
- **le catalogue public** (`catalog/`, livré avec le code, donc à jour à chaque mise à jour D94) :
  - des skills et des fiches de serveurs MCP, où chaque Orion pioche (`skill` : catalog,
    install) ; on y ajoute quelque chose par une pull request ;
  - d'autres catalogues peuvent s'ajouter (`catalog_urls`, un `index.json`). Ce qui en vient
    est du contenu venu d'ailleurs, et une skill installée de là est lue comme telle ;
  - installer un serveur MCP, c'est un changement de réglages qu'Orion propose
    (`configure_change`), avec son jeton demandé par le formulaire du gardien (`secret_ask`).

Limite : en mode séparé, le gardien relaie les appels MCP sans vérifier lui-même le oui du
propriétaire. C'est la politique du cœur qui le demande.

## D96 — Un shell sur le serveur, chaque commande approuvée

*20/10/2026 · acceptée*

`[modules.shell]` (éteint par défaut) donne à Orion l'outil `shell`, pour ce qu'aucun autre
outil ne fait : convertir un fichier, vérifier quelque chose, lancer un script.
- chaque commande est soumise au propriétaire telle quelle avant de tourner. Ce n'est pas une
  action autonomable par défaut ;
- l'allumer est un réglage du propriétaire (`modules.shell`, comme `modules.mcp`, D92) : son
  oui, jamais seul ;
- la commande tourne dans `<data_dir>/workspace`, sous l'utilisateur d'Orion, avec un
  environnement nu (PATH, HOME, LANG) : aucune clé d'Orion ne l'atteint. En mode séparé, le
  cœur n'a de toute façon que la clé du modèle ;
- elle s'arrête après son délai (`max_seconds` au plus), avec tout son groupe de processus. La
  sortie est bornée et compte comme du contenu venu d'ailleurs.

## D97 — Un plan pendant le tour

*20/10/2026 · acceptée*

Sur une tâche longue, Orion se perdait parfois entre les étapes, et le propriétaire attendait
sans savoir où il en était. Désormais :
- `plan` : pour une tâche de trois étapes ou plus, Orion tient la liste de ses étapes, chacune
  à faire, en cours ou faite. Il renvoie la liste entière à chaque avancée : le plan vit dans la
  conversation, nulle part ailleurs, et se rejoue avec elle. Une seule étape en cours à la fois.
  `show` envoie le plan au propriétaire au départ ;
- `plan` passe **avant les autres appels de la même étape** : le plan montré part avant qu'un
  outil lent tourne (événement `Told`). Les résultats restent dans l'ordre des appels. Rien
  n'est envoyé quand personne n'attend (un tour d'initiative).

Le texte qu'Orion écrit à côté d'un appel d'outil est envoyé tout de suite. Un outil `tell`
pour écrire en cours de route a été essayé, puis retiré à la demande du propriétaire.

## D98 — Qui a écrit au bot ; les heures calmes quand il est réveillé

*20/10/2026 · acceptée · corrige D91*

Le 20/10 à 23 h 25, un ami du propriétaire a écrit au bot. Le message a bien été reçu, mais
deux choses ont manqué :
- l'alerte « quelqu'un a écrit au bot » attendait la fin des heures calmes (8 h), alors que le
  propriétaire était en train de parler à Orion ;
- quand il a demandé « tu as reçu des messages de Victor ? », `contacts` ne listait que les
  proches déclarés. Orion a répondu « rien », puis a inventé que le secrétaire était éteint.

Désormais :
- **`contacts`** liste aussi ceux qui ont écrit au bot sans être autorisés, avec leur id, la
  date et leur message, et dit que le secrétaire est allumé. Son contenu est du contenu venu
  d'ailleurs : il cite les messages des autres ;
- **les heures calmes ne retiennent plus Orion** quand le propriétaire a écrit il y a moins de
  20 minutes. Elles sont là pour son sommeil, pas pour interrompre une conversation.
- **une connexion coupée par le réseau est réessayée** (`TelegramClient.call`, deux fois, après
  0,5 puis 2 s) : le 20/10 à 23 h 16, une demande d'accord n'est jamais partie parce qu'une
  connexion inactive avait été coupée, et l'action a été refusée. Seules les pannes de la
  connexion elle-même sont réessayées, pas les refus de Telegram. La longue attente des messages
  (`getUpdates`) reprend déjà d'elle-même.
- **ne pas avoir pu demander n'est pas un refus** : le 20/10 à 23 h 38, l'Orion de Victor ne
  pouvait pas joindre son propriétaire (il n'avait jamais écrit à son bot). Il a donc répondu
  « décliné », deux fois, et l'Orion de Noan a annoncé deux refus. Une approbation porte
  désormais `reached`, faux quand personne n'a pu être interrogé (aucun canal, ou la question
  n'est pas partie). Un rendez-vous reçu est alors refusé avec la raison `unreachable`, et les
  deux propriétaires l'apprennent comme tel ;
- **l'appairage dit d'où il vient** : une invitation créée sur le serveur et acceptée par
  l'autre Orion. Orion ne peut ni créer ni accepter une invitation, et le disait mal quand on
  lui demandait.
- **la journée d'attention va du réveil au réveil** : les messages de Victor relayés entre
  1 h et 2 h comptaient pour le mercredi, et le budget du jour (6) était épuisé avant le
  point de 8 h 30. Ce qui part pendant les heures calmes après minuit compte désormais pour la
  veille (`InitiativeSettings.attention_day`).

## D99 — Un rendez-vous à plusieurs Orion

Le test du 21/10 a marché à deux ; un groupe demandait plus que des propositions en parallèle :
chaque Orion aurait pris le premier créneau libre pour lui, pas pour tous. Un rendez-vous à
plusieurs reste une négociation à deux par invité, liées par un identifiant de groupe :

- **sondage** (`meeting.poll`) : l'Orion qui demande envoie à chacun ses créneaux libres, avec
  le nom des autres invités. Chacun répond par du code (`meeting.available`) lesquels sont
  libres pour lui : c'est ce qu'un refus « aucun créneau » disait déjà à deux, et personne
  n'est dérangé ;
- **proposition** : le premier créneau libre pour tous (et toujours pour lui) est proposé à
  chacun comme une proposition ordinaire, d'un seul créneau. Chaque propriétaire dit oui une
  fois, en voyant avec qui ;
- **confirmation** seulement quand tous ont accepté : un engagement et une ligne d'agenda pour
  le groupe, un seul message au propriétaire. Un refus annule chez ceux qui avaient dit oui
  (`meeting.cancel`), qui retrouvent leur créneau, et le propriétaire apprend qui et pourquoi.

Le gardien tient chaque étape : un seul oui du propriétaire couvre les sondages du groupe, en
nommant tous les invités (`also`) ; après un sondage, il ne laisse partir qu'un créneau sondé ;
une disponibilité ne répond qu'à un sondage reçu ; seul celui qui a proposé peut annuler.

Pas encore : un invité qui ne répond jamais bloque le groupe (pas de délai).

## D100 — Le forfait Claude par Claude Code

Noan voulait brancher son abonnement Claude. Les conditions d'Anthropic réservent la connexion
par compte Claude à Claude Code et aux applications d'Anthropic : une application tierce ne
doit ni recueillir ni faire transiter ces identifiants. Elles permettent en revanche à
l'utilisateur de se connecter lui-même au binaire Claude Code non modifié, pour un usage
individuel. Orion ne touche donc à aucun jeton : il lance `claude --print`, connecté par son
propriétaire (`claude login` sur le serveur), modèles `claude-code:<modèle>`.

- Claude Code ne prête rien de lui-même : aucun outil (`--tools ""`), ni réglages, ni serveurs
  MCP, ni session gardée, un dossier vide comme répertoire de travail, et le message système
  d'Orion à la place du sien (`--system-prompt-file`).
- Les outils d'Orion voyagent en texte dans ce message système, triés, toujours pareils ; la
  réponse est un objet JSON vérifié par un schéma (`--json-schema`) : le texte et les appels
  d'outils. La conversation passe sur stdin, rendue à l'identique d'un appel à l'autre : le
  préfixe mis en cache chez Anthropic ne bouge pas.
- Ce qu'il ne sait pas faire ici (images, recherche web) est refusé pour que le routeur passe à
  un autre fournisseur, comme un forfait épuisé.
- Si Orion est distribué, chaque utilisateur se connecte avec son propre compte ; Orion n'en
  revend ni n'en partage jamais l'usage.

## D101 — Le tableau de bord

La console montrait déjà ce qu'Orion sait et ce qu'il a fait, page par page. Le tableau de
bord répond d'un coup d'œil à quatre questions, à partir du journal seul : combien de son
attention du jour Orion a pris, et pour quoi (ce qui était demandé ne compte pas) ; ce qu'il a
choisi de taire, et pourquoi (budget épuisé, pas assez important, trop tard), pour que Noan
puisse dire « ça, il fallait me le dire » ; ce qu'il a fait hors de lui-même sans demander ; et
ce qu'il a coûté sur sept jours, avec la part relue depuis le cache. Pas de script : des barres
en HTML, comme le reste de la console, lisibles sur un téléphone.

## D102 — Les Orion se parlent librement

Jusqu'ici, deux Orion ne pouvaient échanger que des rendez-vous. Noan voulait qu'ils discutent,
et que ce soit Orion lui-même qui réponde, avec tout ce qu'il sait, plutôt qu'un appel séparé
sans données comme le secrétaire de ses proches : le risque d'une fuite est accepté en
connaissance de cause, pour une conversation qui sert vraiment.

- `orion.say` : quelques mots libres (2 000 caractères au plus), signés comme le reste.
- Écrire le premier (`peer_message`) : le propriétaire approuve les mots exacts, et le gardien
  vérifie ce oui. Répondre ne demande rien : le gardien laisse partir une réponse dans les six
  heures qui suivent un message de cet Orion, comme Orion répond à quelqu'un qui lui écrit.
- Orion répond dans un fil à part pour chaque Orion (`peer:<instance>`), avec sa mémoire et ses
  outils. Ce que l'autre écrit garde la confiance d'un pair : toute action sur le monde que la
  conversation lui ferait faire passe par l'accord du propriétaire, comme pour un mail.
- La conversation a une fin : chaque Orion peut répondre `SILENCE`, et le gardien arrête à 30
  messages par jour vers un même Orion.
- Le propriétaire en entend parler : ce qui a été dit et répondu lui arrive comme un message de
  l'initiative, sous le budget du jour.

## D103 — Moins de texte en dur, des accords qui disent d'où ils viennent

Le premier test des conversations entre Orion (21/10) a montré deux défauts :

- **une demande d'accord sortie de nulle part** : l'Orion de Victor, en discutant avec celui de
  Noan dans un fil que Victor ne voit pas, lui a demandé trois fois d'approuver une proposition
  sans rien expliquer. Une demande née d'une conversation qu'il ne voit pas dit maintenant ce
  qui l'a amenée (« l'Orion de Noan m'a écrit « … » »). Les deux premières avaient échoué parce
  que les rendez-vous s'arrêtaient à 19 h en semaine : un ciné ou un dîner finit plus tard, le
  défaut passe à 23 h et le week-end compte ; un échec dit les heures, pour que le modèle le
  dise au lieu de redemander ;
- **des messages en dur** : quand le vérificateur refusait un brouillon, le texte de secours
  partait, et pour la fédération c'était la consigne écrite pour Orion (« rappelle-lui de… »).
  Orion réécrit désormais une fois avec la remarque du vérificateur ; le texte de secours ne
  sert qu'en dernier recours, et il est écrit pour le propriétaire. Le vérificateur voit aussi
  le début de ce qu'Orion a lu (sa mémoire), plus seulement la fin : un vrai fait de mémoire
  était pris pour une invention.

## D104 — Le budget d'attention ne compte que ce qu'Orion dit de lui-même

Six messages par jour, c'était trop bas pour un usage réel, et le 21/10 l'a montré : les
messages de Victor relayés la nuit ont tout pris, et le point de 8 h 30 a sauté. Or quand
quelqu'un écrit à Noan (un proche, par le secrétaire, ou un autre Orion), Orion ne décide pas
de le déranger : il transmet. Ces messages-là ne comptent plus (`Alert.spoken_to`) ; les heures
calmes et l'arbitre (il a l'air occupé) s'appliquent toujours. Le budget ne limite plus que les
initiatives d'Orion (points du jour, idées, alertes), et passe à 12 par défaut. Le tableau de
bord marque à part ce qu'on lui a écrit.

## D105 — Installer et retirer des modules natifs

Un module tiers pouvait déjà exister (un paquet Python sous `orion.modules`), mais rien ne
permettait de l'installer, et la mise à jour automatique (`uv sync --frozen`) l'aurait effacé
au passage suivant, laissant Orion refuser de démarrer. Désormais :

- **un catalogue** (`catalog/modules/<nom>.toml`) : description, ce que le module atteint, ce
  qu'il faut régler, et une source à une seule version exacte (`path:` dans le dépôt,
  `paquet==version`, ou `git+…@commit complet`) ; une source non épinglée n'est pas proposée ;
- **un dossier à part** : chaque module dans `data/modules/site/<nom>`, listé dans
  `installed.toml`, ajouté au chemin d'Orion après ses propres paquets. L'environnement du code
  n'est pas touché : une mise à jour les garde, et retirer un module, c'est effacer son dossier ;
- **installer** : dans un dossier temporaire ; un autre processus Python vérifie que le point
  d'entrée se charge et que c'est bien le module annoncé ; puis il prend sa place, et
  l'interrupteur `modules.<nom>.enabled` est écrit si Orion démarre avec (sinon il reste éteint
  et dit ce qu'il faut régler) ; Orion redémarre ;
- **qui fait quoi** : un module tourne dans le cœur, avec ses accès, la mémoire comprise. C'est
  donc le cœur qui l'installe : les scripts d'un paquet tournent avec exactement les droits
  qu'aura le module, jamais près des secrets. Le gardien n'importe jamais le code d'un module :
  il sait seulement lesquels sont installés. Il vérifie le oui du propriétaire et, sous ce même
  oui, écrit l'interrupteur, et rien d'autre ;
- **le oui** : `module_install` et `module_remove` sont toujours demandés au propriétaire
  (jamais seuls, quelle que soit l'autonomie listée), jamais depuis une conversation qui
  contient du contenu extérieur ; la question, écrite par le gardien à partir du catalogue,
  montre la source exacte et rappelle que ce code aura accès à sa mémoire ;
- **retirer** : l'interrupteur passe à faux, puis le dossier est effacé ; une section éteinte
  laissée par un module retiré n'empêche plus Orion de démarrer ;
- sur le serveur : `orion-admin --core modules list|install|remove <nom>`.

Un module d'exemple, `des` (`examples/modules/orion-des`), montre la forme : il lance des dés
au hasard, sans réseau ni mémoire. Les modules isolés (hors du processus d'Orion, sans accès à
sa mémoire), pour une bibliothèque ouverte à tous, viendront ensuite.

## D106 — Une mission arrêtée s'arrête tout de suite, et démarre tout de suite

Constaté en vrai : une mission lancée (« j'y travaille maintenant ») n'a commencé qu'au tour
suivant du planificateur, huit minutes plus tard ; et quand il a dit « laisse tomber » pendant
qu'elle tournait, Orion a répondu « je laisse tomber » mais le passage en cours a continué une
quinzaine d'étapes et lui a envoyé une capture après coup. Désormais :

- **démarrer réveille le travail des missions** : le premier passage part aussitôt ;
- **la boucle d'Orion demande, avant chaque étape, si le travail a été annulé** (`halted`) : une
  mission close pendant son passage s'arrête là, les appels prévus ne partent pas, rien n'est
  dit ni rapporté. Un appel déjà en cours n'est jamais coupé net.

## D107 — Les photos d'une page lue peuvent être envoyées telles quelles

Constaté en vrai : il voulait les photos des colliers trouvés, et Orion a passé trois minutes à
deviner l'adresse des images, refusées une à une (une adresse inventée peut faire sortir ses
données dans l'URL), avant de se rabattre sur des captures. Désormais, les images d'une page
qu'Orion a lue (`web_read`) ou ouverte dans son navigateur sont relevées par le code, sur la page
elle-même : l'image d'aperçu (`og:image`), puis les photos (sans logos, icônes ni images de
remplissage ; dans le navigateur, les plus grandes affichées). Elles lui sont montrées avec leur
adresse, et il peut les télécharger pour les envoyer. Il ne peut toujours pas télécharger une
adresse inventée.

## D108 — « Je continue » doit être vrai : un passage de mission tout de suite

Constaté en vrai : « fais tout maintenant », et Orion répond « je continue sur les autres
maintenant »… puis rien, car une fois sa réponse partie il ne travaille plus que dans une
mission, et celle-ci repassait toutes les 6 h. Désormais `mission` (now) demande un passage tout
de suite : depuis la conversation (il le veut maintenant), ou depuis un passage qui a encore des
pistes (le suivant part juste après). Huit par mission et par jour au plus, au-delà de son rythme ;
le plafond de dépense reste celui de ce qu'Orion fait seul. Et la consigne : ne dire « je
continue » que si c'est vrai.

## D109 — Ce qui s'est passé ailleurs pendant ce temps

Constaté en vrai : « tu as bossé pendant mon absence ? » → « non, rien », alors qu'une mission
avait tourné et lui avait envoyé une capture. Sa conversation montre ce qui s'y est dit, pas ce
qu'Orion a fait ailleurs. Désormais, quand il écrit, le journal qui suit le dernier message de
la conversation est relu, et ce qui s'est passé hors d'elle est joint à son message, daté
(`<pendant_ce_temps>`) : passages de mission et leurs rapports, messages et fichiers envoyés
d'ailleurs, réponses du secrétaire à ses contacts, échanges avec les autres Orion, rendez-vous
calés ou annulés, accords demandés, réflexions de la nuit, envois ratés. Ce sont des faits du
journal : rien à deviner pour le modèle. Ce que la conversation montre déjà n'y est pas, et
rien n'est joint quand rien ne s'est passé.

## D110 — Les tâches de fond : un aide qui ne parle qu'à Orion

Constaté en vrai : sur une grosse recherche, Orion cherchait dans sa réponse, gonflait la
conversation (45 000 jetons relus à chaque étape) et finissait sur « je continue », sans suite.
Il voulait que ces travaux tournent à part, et que l'aide échange avec Orion plutôt qu'avec lui.

- `task` (start) confie le travail à un aide : Orion lui-même, dans son propre fil
  (`task:<id>`), avec une consigne complète. Il travaille seul, passage après passage, jusqu'à
  `task` (done) ou la fin de son budget (dollars, minutes, 20 passages au plus).
- **Il n'écrit qu'à Orion.** Ce qu'il dit arrive à Orion dans la conversation du propriétaire
  (une alerte passée telle quelle, hors du budget d'attention) : Orion choisit ce qui lui
  parvient, transmet ses fichiers, lui pose une question s'il le faut, ou ne dit rien. Orion
  lui répond ou lui transmet les nouveaux souhaits avec `task` (say), l'arrête avec (stop).
  L'aide peut attendre la réponse d'Orion (`task` wait).
- **Le gardien le cantonne à la recherche** : web, navigateur, téléchargement de fichiers, PDF.
  Jamais un message, un accord ni un acte vers le propriétaire ou le monde depuis une tâche :
  il le demande à Orion.

Budget du prompt relevé (38,5 k et 41 k) après avoir allégé les missions : c'est une capacité
de plus, voulue.

## D111 — Un coffre d'identifiants, comme KeePass

*22/10/2026 · acceptée · complète D49 et D94*

Une session prêtée (D49) expire, et il fallait alors se reconnecter à la main, sur un
ordinateur avec un écran. Le propriétaire voulait qu'Orion se connecte lui-même, sur n'importe
quel site. Le gardien garde donc aussi, comme KeePass, un identifiant par site : l'adresse,
l'identifiant, le mot de passe et, s'il le veut, la clé de double authentification
(`orion.guardian.logins`).

- **N'importe quel site** : l'agent d'une tâche approuvée (`computer_task_send`, avec `login`)
  conduit la page de connexion comme toute autre page (bandeau de cookies, « Se connecter »,
  deux étapes…). Il demande à `fill_login` de taper l'identifiant, le mot de passe ou un code
  dans le champ qu'il désigne. Pas de recette par site.
- **Le modèle ne voit jamais un secret** :
  - c'est le code qui tape, pas le modèle ;
  - chaque page lue par l'agent a le mot de passe et les codes masqués, tels quels ou encodés
    dans une adresse ; son compte rendu aussi ;
  - le journal ne les contient pas.
  Une page piégée ne peut donc pas lui faire recopier le mot de passe ailleurs.
- **Seulement au bon endroit** : rien n'est tapé hors des sites de l'identifiant, et le mot de
  passe ne va que dans un champ mot de passe.
- **La clé 2FA ne quitte jamais le gardien** : l'agent lui demande le code du moment, avec
  l'identifiant du prêt, tant que la tâche dure (une heure au plus).
- **Prêté comme une session** : pour une tâche dont le propriétaire a approuvé la consigne et
  qui nomme cet identifiant, une seule fois ; ou faite seule, dans une autonomie dont les sites
  couvrent tous ceux de l'identifiant. Un même oui prête la session et l'identifiant. Chaque
  prêt et chaque code sont notés dans le registre du gardien.
- **Comment il les reçoit** :
  - depuis son téléphone : Orion demande (`computer_login_ask`) et le gardien lui envoie son
    propre formulaire (D94) ;
  - sur le serveur : `orion logins add` (le mot de passe est saisi, jamais en argument) ;
  - en important l'export CSV de son gestionnaire (KeePass, KeePassXC, Bitwarden, Chrome,
    Firefox, 1Password) : `orion logins import`.

Ce qui reste hors de portée : un code envoyé par SMS ou par mail, un captcha, une validation
dans l'application du site. L'agent s'arrête et le dit. Les secrets sont dans des fichiers 0600
du gardien, non chiffrés : une clé posée sur la même machine ne protégerait de rien. Garder la
clé 2FA d'un site met les deux facteurs sur le serveur : le propriétaire le choisit site par
site.

    guardian.login.added  guardian.login.removed  guardian.login.lent  guardian.login.code

## D112 — Se connecter depuis Telegram, sans deuxième installation

*22/10/2026 · acceptée · complète D49 et D111*

Prêter une session (D49) demandait `orion login` sur un ordinateur avec un écran, donc un
deuxième Orion installé quelque part. Le propriétaire n'a que son téléphone sous la main, et
c'était un frein. Désormais, Orion demande (`computer_login_ask`, par défaut une session), et
le gardien envoie sur Telegram un bouton qui ouvre **sa propre Mini App** (`/secrets/browse/`,
`orion.guardian.remote`) :

- l'image d'un navigateur que le gardien fait tourner sur le serveur, qui se présente comme
  celui d'un téléphone. Il touche l'image pour cliquer, tape le texte en dessous, puis « J'ai
  fini » ;
- il se connecte comme sur son téléphone : code reçu par SMS, captcha et validation dans
  l'application compris, ce que l'agent ne sait pas faire (D111) ;
- le gardien garde la session (cookies, et le téléphone qui l'a faite : un site lie souvent sa
  session au navigateur), comme `orion sessions add`. Rien de ce qu'il tape n'est noté ;
- le cœur n'est jamais devant : tout passe par l'écoute publique du gardien. Chaque action est
  vérifiée sur les données de lancement signées par Telegram (le propriétaire, ce bot), l'image
  n'est servie qu'une fois la page ouverte par lui, et le navigateur se ferme quand il a fini,
  qu'il abandonne, ou après 10 minutes sans rien toucher. Comme celui du cœur, il n'atteint
  jamais une adresse privée.

`orion login` reste possible sur un ordinateur. Le gardien a maintenant besoin du navigateur
(`PLAYWRIGHT_BROWSERS_PATH` dans `orion-guardian.service`).

Au passage, la page qui ouvre la console depuis Telegram restait sur « Un instant… » dès que
quelque chose échouait (le script de Telegram absent, le serveur muet, une erreur). Elle dit
maintenant pourquoi.

## D113 — Plus d'agent web : Orion fait lui-même, dans son navigateur

*22/10/2026 · acceptée · révise D49, D73, D111 et D112*

Constaté en vrai : pour tester sa connexion à Hyperplanning, Orion a confié une tâche à
l'agent web au lieu de le faire. Le propriétaire n'en voulait plus : « je veux qu'il fasse
lui-même ». L'agent web (`computer_task`, `computer_task_send`) disparaît. Le navigateur
d'Orion (`browser`) fait tout, de bout en bout, sous ses yeux :

- **Lire** reste libre : les règles du navigateur tiennent (adresses autorisées, `type`
  limité aux mots du message, rien ne part).
- **Se connecter à un compte** (`browser` login) : avec sa session ou son identifiant gardé.
  Le gardien les prête sur son oui (ou dans une autonomie dont les sites couvrent ceux du
  compte), comme avant. Avec un identifiant, `browser` fill tape l'identifiant, le mot de
  passe ou le code 2FA sans qu'Orion les voie (masqués sur chaque page lue et dans le
  journal), et pendant 5 minutes le navigateur peut envoyer aux sites du compte, et à eux
  seuls, le temps de se connecter.
- **Envoyer** (`browser` send) : réserver, commander, remplir un formulaire. Le propriétaire
  approuve exactement : la page, les textes tapés, ce que ça fait. L'envoi ne part que vers le
  site de cette page, le temps du clic.
- **Ce qui protège** : les pages qu'Orion lit rendent son contexte non fiable, donc chaque
  connexion ou envoi après une page lue lui est demandé (D26), jamais fait seul. Une tâche de
  fond (D110) ne fait que lire : ni connexion ni envoi.
- **Les réservations** (D73) : Orion réserve lui-même, puis dit ce que le site a répondu
  (`booking` done). Le code range alors le rendez-vous dans l'agenda avec ses rappels.
- `browser` accounts liste les sessions et identifiants gardés, `browser` connect lui demande
  de se connecter depuis son téléphone (D112) ou de confier son mot de passe (D111).

Ce qu'on perd : la séparation entre le modèle qui sait tout de lui et celui qui lit les pages.
Elle est remplacée par son accord à chaque envoi, avec le texte exact, et par le cloisonnement
du réseau, qui tiennent quoi que dise une page.

## D114 — Un seul Orion : plus d'intermédiaire entre ses informations et lui

*22/10/2026 · acceptée · révise D26 (en partie), D63, D87 et le secrétaire*

Le propriétaire ne voulait plus que ses informations soient traitées par une autre version
qu'Orion. Quatre intermédiaires disparaissent :

- **Le web** : il n'y a plus de lecteur à part. `web_read` montre à Orion la page elle-même,
  partie par partie (ou là où apparaît un mot), avec ses liens et ses images. `web_search`
  passe par le moteur du fournisseur, lancé avec le modèle d'Orion, et ne rend que les pages
  trouvées (adresse et titre), jamais un résumé. Le texte des pages n'est pas journalisé.
- **Le mail** : plus de quarantaine. Chaque mail est journalisé avec son texte (8 000
  caractères au plus). Chaque nouveau mail qui n'est pas une newsletter arrive à Orion, qui le
  lit, juge lui-même s'il y a quelque chose pour son propriétaire, et note lui-même une
  échéance ou un engagement. `mail` read en donne le texte entier.
- **Les documents** : un PDF ou un fichier Office qui contient du texte est lu par Orion
  lui-même. Le code en extrait le texte, et `attachment_look` en montre la suite. Les photos,
  Orion les regardait déjà lui-même (D87). Le modèle qui voit ne sert plus qu'en secours :
  pour un scan sans texte, un ancien format, ou si le modèle principal ne voit pas les images
  (`direct = false`).
- **Les proches** : c'est Orion qui leur répond, avec toute sa mémoire et tous ses outils,
  dans une conversation à part pour chacun (`contact:…`), comme aux autres Orion (D102). Il ne
  leur dit que ce que son propriétaire voudrait qu'ils sachent, et lui transmet ce qui compte
  (`contact_note`).

Ce qui protège désormais des pages, des mails et des messages piégés : ils gardent leur niveau
de confiance (WEB, tiers), et toute action sur le monde après les avoir lus est demandée au
propriétaire (D26, D90). Les règles du réseau (adresses autorisées, rien ne part sans son oui)
tiennent quoi qu'ils disent.

Restent pour l'instant : la mémoire, la nuit, la réflexion, les résumés, la chronique, la
curiosité, le vérificateur et l'expert (à décider). La transcription des vocaux, la voix et
la génération d'images sont des outils, pas des esprits. Le modèle de secours ne prend le
relais que si le principal est en panne.

## D115 — Le navigateur d'Orion sans garde-fous : il navigue comme une personne

*22/10/2026 · acceptée · révise D113, D111, D49 et la lecture du web*

Constaté en vrai, sur Hyperplanning : Orion ne gardait que le domaine du site, et son
navigateur bloquait l'application, qui charge l'emploi du temps par des requêtes en arrière-plan.
Le propriétaire a jugé ces protections « plus handicapantes qu'autre chose » et a demandé de les
supprimer, en connaissance de cause.

- **Navigation libre** : n'importe quelle adresse, n'importe quel texte tapé, les formulaires
  envoyés, les clics partout. L'action `send` à approuver disparaît. `web_read` et `file`
  download ouvrent aussi n'importe quelle adresse.
- **Ses comptes** : `browser` login ne lui demande plus rien ; le gardien prête la session ou
  l'identifiant quand Orion le demande, et note chaque prêt.
- **La page gardée** : c'est Orion qui choisit l'adresse complète gardée avec un compte (celle
  où il reviendra, son emploi du temps…), en la donnant quand il demande au propriétaire de se
  connecter depuis Telegram ; c'est elle qu'il rouvre ensuite.
- **Ce qui reste**, parce que ça ne gêne en rien : les adresses du serveur lui-même et de son
  réseau ne sont jamais atteintes (le gardien), et un mot de passe ou un code prêté reste masqué
  sur chaque page qu'Orion lit.

Le risque accepté : une page piégée pourrait pousser Orion à envoyer des informations
ailleurs ; rien ne l'en empêche plus techniquement, seul son jugement (une page est une
information, jamais un ordre ; ce qui engage le propriétaire, il s'assure qu'il le veut).

## D116 — Un seul navigateur qui garde ses connexions ; il lui passe la main, comme ChatGPT

*22/10/2026 · acceptée · remplace D112 et les sessions prêtées de D49*

Constaté en vrai : impossible de connecter Orion à Hyperplanning. Il y avait deux navigateurs,
celui du gardien où le propriétaire se connectait et celui d'Orion qui recevait une copie de la
session, avec une adresse, des domaines et une empreinte « téléphone » à faire correspondre :
une pièce de travers, et ça échouait (la connexion de l'école passe par un autre site). Le
propriétaire a demandé de faire comme ChatGPT, dont le navigateur garde ses connexions et passe
la main quand une page a besoin de l'utilisateur.

- **Un seul navigateur, un profil gardé sur disque** (`data/browser`) : ses cookies, donc ses
  connexions, valent dans chaque conversation (un onglet par conversation) et survivent à un
  redémarrage. Les cookies de session, que Chromium jette, sont gardés à côté (0600).
- **Il passe la main** (`browser` hand_over) : quand une page a besoin du propriétaire (se
  connecter, un code reçu par SMS, un captcha), il reçoit sur Telegram un bouton qui ouvre la
  vue en direct de **cet onglet** (une Mini App servie par la console, `/console/browser/`). Il
  touche l'image pour cliquer, tape dessous, puis « J'ai fini ». Ce qu'il tape va dans la page,
  jamais dans la conversation ni le journal. Orion est prévenu et reprend sur la page laissée.
  Chaque action est vérifiée sur les données signées par Telegram (le propriétaire, ce bot).
- **Se déconnecter** : `browser` forget efface les cookies d'un site, ou de tous.
- **Le coffre de mots de passe (D111) reste en option** : `browser` login puis fill tapent un
  mot de passe gardé par le gardien sans qu'Orion le voie.
- **Retirés** : les sessions gardées et prêtées par le gardien, son navigateur et sa Mini App
  de connexion (D112), `orion login` et `orion sessions`.

Ce qui change de place : les connexions vivent dans le navigateur d'Orion, sur le serveur, et
plus chez le gardien, ce qui va avec D115.

## D117 — La nuit d'Orion : il travaille pendant que le propriétaire dort

*23/10/2026 · acceptée · révise D51 (la lecture de nuit), complète D110 et D114*

Constaté en vrai : une nuit sans rien, alors que la journée laissait des choses en plan. Le
propriétaire voulait qu'Orion soit curieux, voie ce qui manque, cherche et continue ce qui
n'était pas fini. La lecture de nuit de D51 ne le pouvait pas : un modèle à part, sans outils,
qui rendait au plus deux pensées en JSON.

- **Orion lui-même, avec ses outils** : à l'heure de la nuit (`[modules.thinking] at`, 5 h),
  Orion relit la journée dans son propre fil (`night`, qui garde la mémoire des nuits
  précédentes). Il regarde ce qui tourne déjà (tâches, missions), reprend ce qui est resté en
  plan, cherche et lit lui-même ce qui tient en quelques recherches, confie le long à une
  tâche de fond, avance une mission, prévoit les nouvelles à prendre, suit une piste à lui.
  Une question promise (`think_later`) reçoit toujours une réponse.
- **Un modèle fort, un budget** : la nuit garde son modèle (`model`, par défaut
  `strong_model`) et un plafond par nuit (`budget_usd`, 2 $). Le plafond de dépense (D79) la
  compte comme du travail de fond. `max_thoughts` et `answer_tokens` disparaissent ; `points`
  dit combien de choses tient la note du matin (3).
- **Rien ne part la nuit** : le gardien refuse dans le fil `night` toute action sur le monde,
  l'envoi d'un fichier (il le recevrait tout de suite) et les réglages de l'initiative, et il
  ne demande jamais d'accord au propriétaire. Ce qui demande son oui, Orion le lui propose au
  réveil. Lire le web et le navigateur reste permis.
- **La note du matin** : ce qu'Orion dit à la fin de sa nuit (ou `SILENCE`) passe par le
  moteur d'initiative après les heures calmes (budget, arbitre, vérificateur), avec la
  confiance de ce que la nuit a lu. Ce que ses tâches de fond trouvent lui parvient à part.

Éval `la_nuit_reprend_ce_qui_reste` : une recherche laissée pour le lendemain, reprise la nuit.

## D118 — Il va au bout, et se lance seul

*23/10/2026 · acceptée*

Le propriétaire trouvait Orion pas assez autonome. Constaté sur un but dit en passant (« toujours
rien de bien pour l'appart ») : Orion proposait une mission et finissait sur « tu me dis si je
lance ? », trois fois sur trois. Ce que recommandent les guides d'agents (persistance jusqu'à la
fin de la demande, relire la demande avant de répondre, ne pas laisser le modèle seul juge de
s'il a fini) :

- **La constitution** : il va au bout avant de rendre la main ; devant une incertitude, il
  cherche ou prend l'hypothèse la plus raisonnable et la dit en une phrase. Une question
  seulement si se tromper coûterait cher ou ne se rattraperait pas, ou pour un choix qui
  n'appartient qu'à lui. Il ne finit pas sur une offre qu'il peut tenir sans risque ; ce qui
  demande un accord, il le fait quand même et l'outil le demande. Avant de répondre, il relit
  la demande ; si une suite s'impose, il s'en occupe avec ses outils (le court, le faire ; le
  long, une tâche ; plusieurs jours, une mission ; une échéance, un rappel), et sa réponse n'en
  dit qu'une phrase. Une première version (« pense à la suite ») rallongeait les réponses de
  conseils et de questions : la contradiction de `veille_de_partiel` s'y noyait.
- **Un contrôle dans le code** (`orion.cognition.finish`) : quand la réponse à un message du
  propriétaire finit sur une offre (« tu veux que je… », « je peux te chercher… ? »), elle ne
  part pas encore ; une note lui demande de le faire d'abord, ou de réécrire la réponse telle
  quelle si c'est un vrai choix. Une fois par tour, journalisé (le cache tient). Pas pour les
  messages d'initiative, où une réponse en un mot est voulue.
- **Tâches et missions de lui-même** : il en lance une quand il voit un besoin que le
  propriétaire n'a pas formulé, et le lui dit ; le propriétaire la ferme d'un mot.

Budget du message système relevé (39,2 k). Évals `se_lance_seul_sur_un_but` (0/3 avant, 3/3
après) et `va_au_bout_au_lieu_de_proposer` (garde-fou : passait déjà). `envoie_moi_ce_xml`
vérifie désormais que la page n'est lue qu'après le téléchargement et l'envoi : lire après un
envoi raté est un repli, pas le défaut visé.

## D119 — Le navigateur lit la page telle qu'elle est

*23/10/2026 · acceptée · complète D113 et D116*

Constaté en vrai sur Hyperplanning : Orion disait être bloqué sur « Veuillez patienter » pendant
que ses captures montraient le formulaire de connexion, et le répétait après chaque clic.
Reproduit sur la démo de PRONOTE (même éditeur) : à l'ouverture, Orion lisait une page vide.
Quatre défauts :

- **Lue trop tôt** : la page était lue dès son HTML chargé, avant que l'appli ne se construise,
  et après un clic qui n'ouvre pas de page (une connexion envoyée en arrière-plan) avant même
  sa réponse. Désormais, après chaque action, le navigateur attend que la page ne change plus,
  qu'aucune requête ne soit en cours depuis un instant et qu'elle dise autre chose que
  « veuillez patienter » ou « chargement » (8 s au plus). Sinon, Orion le lit avec la page :
  qu'il la relise avant de dire qu'elle bloque.
- **Relue de mémoire** : `browser` read rendait le texte gardé de la dernière action, pas la
  page. Il la relit maintenant à chaque fois (toute la page quand elle tient en une partie).
- **Les cadres** : le texte et les éléments ne venaient que du document principal. Ceux des
  cadres visibles (un formulaire de connexion d'école, de paiement) sont lus et numérotés à la
  suite, et `fill` vérifie le site du cadre du champ.
- **Un élément redessiné** : une appli qui se redessine efface les numéros ; le clic attendait
  8 s puis échouait sans dire pourquoi. Il dit maintenant que la page a changé et qu'il faut la
  relire.

La consigne du navigateur ajoute : un formulaire déjà rempli (« se souvenir de moi »), Orion
clique lui-même sur se connecter avant de lui passer la main. Éval `une_appli_qui_se_construit`
sur la démo PRONOTE (2/2 en 20 s ; avant, 1/2 en 70 s). `mon_linkedin` et `urssaf_a_ma_place`
suivent D116 (plus de session prêtée ni d'`orion login`).

## D120 — Le coffre sans le gardien, et « garder cet identifiant ? »

*23/10/2026 · acceptée · complète D111*

Le propriétaire voulait qu'Orion se serve d'un coffre comme KeePass : taper ses mots de passe
sans les voir. Le coffre de D111 le faisait déjà, mais seulement quand le gardien tourne à
part (D28), ce qui n'était pas son cas : Orion répondait « le gardien ne tourne pas ».

- **Orion garde le coffre lui-même** quand le gardien ne tourne pas à part
  (`orion.computer.vault`). Ce sont les mêmes fichiers (`guardian/logins/*.json`, 0600) et le
  même registre : `orion logins add` et `orion logins import` (l'export CSV de KeePass,
  Bitwarden, Chrome…) le remplissent dans les deux cas. Quand le gardien tourne, c'est lui qui
  prête, comme avant.
- **Ce qui ne change pas** : le modèle ne voit jamais un mot de passe. Le code le tape, sur les
  sites de l'identifiant seulement et dans un champ mot de passe seulement ; il est masqué sur
  chaque page qu'Orion lit ; aucun journal ne le garde.
- **Ce que ça coûte** : le mot de passe est lu par le processus d'Orion, et non plus prêté par
  un autre. Une faille dans le code d'Orion pourrait l'atteindre ; le modèle, toujours pas.
  Le propriétaire l'a choisi pour se passer du gardien.
- **« Garder cet identifiant ? »** : quand le propriétaire se connecte lui-même dans une
  passation (D116), ce qu'il tape dans un champ mot de passe est gardé en mémoire (et masqué
  pour Orion). S'il finit et que la connexion est passée (plus de champ mot de passe sur la
  page), un bouton Telegram lui propose de le garder ; rien n'est gardé sans son oui. Quand le
  gardien tourne à part, c'est lui qui le garde (`/v1/logins/keep`) : le cœur lui passe
  l'identifiant après le oui, et le nomme comme le coffre local (`keep_login`).
- **Depuis son téléphone** : `browser` save_password lui envoie un formulaire (servi par la
  console, vérifié par Telegram) pour donner un identifiant ; il va dans le coffre.
- Orion regarde son coffre avant de passer la main pour une connexion.

## D121 — La boucle écoute jusqu'au bout, dit ce qu'elle fait, et ne s'arrête plus net

*25/10/2026 · acceptée*

Sur Telegram, trois choses rendaient Orion moins fiable qu'un agent comme Claude Code : un
message écrit pendant qu'il rédigeait sa réponse arrivait trop tard, un long tour restait muet
si le modèle ne commentait pas, et un tour à court d'étapes s'arrêtait net.

- **Une réponse pas encore partie est retenue** (`orion.cognition.loop`). Quand le modèle a
  fini, la boucle regarde si le propriétaire a écrit entre-temps, comme elle le fait déjà entre
  deux appels d'outils (D23). S'il l'a fait, la réponse ne part pas : son message et une note
  (`UNSENT`) rejoignent la conversation, et le modèle répond aux deux. S'il ne reste pas
  d'étape ou de budget, la réponse part et le message ouvre le tour suivant.
- **Un long tour dit ce qu'il fait.** Après 10 s, si le modèle n'a rien dit avant ses appels
  d'outils, Orion envoie ce que font les outils (`Tool.doing` : « je cherche sur le web… »),
  au plus une fois toutes les 30 s et jamais deux fois la même phrase. Les outils sans
  étiquette donnent une phrase neutre, une seule fois.
- **Un tour à court d'étapes continue une fois** (`[agent] max_continuations`, 1 par défaut),
  pour les messages du propriétaire seulement, en le lui disant (« c'est plus long que prévu,
  je continue »). Le plafond de coût du tour tient toujours ; un tour qui tourne en rond
  (appels répétés) ne continue jamais.
- **Ce qui ne change pas** : chaque requête prolonge la précédente octet pour octet (le cache
  de prompt) ; les phrases de progression ne sont pas dans la conversation.

## D122 — Une boucle qui ne bute plus sur les mêmes murs

*25/10/2026 · acceptée*

Comparée au code public d'autres agents (Codex CLI, OpenHands, Cline, opencode), la boucle
d'Orion avait quatre angles morts sur les tâches longues. On reprend ce qu'ils font de mieux,
sans rien changer à la confiance ni au cache de prompt.

- **Un appel répété se reconnaît même retapé** : le compteur lit les arguments comme des
  données (clés triées, sans espaces), plus comme du texte. Un `{"text": "a"}` et un
  `{"text":"a"}` sont le même appel.
- **Le même échec deux fois est dit sans espoir** (comme le rappel d'OpenHands,
  `stuck_detector.py`) : quand un outil renvoie deux fois la même erreur, même avec d'autres
  arguments, son résultat dit une fois que réessayer ne changera rien.
- **Une sortie trop longue garde son début et sa fin** (comme Codex,
  `output-truncation`) : le milieu est coupé, avec ce qui l'a été à sa place. Les totaux, les
  erreurs et les conclusions sont souvent à la fin.
- **Une réponse coupée par la limite de longueur est continuée** (comme Cline) : jusqu'à deux
  fois, le modèle reprend où elle s'arrête, et le propriétaire la reçoit entière.

Déjà en place, et gardé : les nouvelles tentatives chez le fournisseur et les modèles de secours,
le résumé structuré quand la fenêtre se décale, le bilan à court d'étapes, la pagination des
pages.

## D123 — Moins de tokens à chaque appel

*26/10/2026 · acceptée*

Chaque appel portait une partie fixe d'environ 70 000 caractères (constitution et schémas des
outils), puis la fenêtre de conversation, qui pouvait monter jusqu'à 120 000 tokens avant le
rebase. Le fil du propriétaire ne se termine jamais : la fenêtre est presque toujours grosse,
elle repart à chaque étape d'une boucle, et après une pause le cache du fournisseur a expiré,
donc tout est repayé plein tarif.

- **Après une pause, une fenêtre légère** : au bout de `fresh_idle_minutes` (60) sans message,
  la conversation repart de ses derniers échanges, dans `fresh_window_tokens` (16 000), avec le
  résumé de ce qui sort, comme un rebase. Le cache est perdu de toute façon : ce décalage ne
  coûte rien. Une conversation courte reste entière.
- **Les vieux gros résultats partent plus tôt** : jusqu'ici raccourcis seulement après une
  pause (pour ne pas casser le cache), ils le sont aussi dès qu'ils dépassent 40 000 caractères
  au total, hors des deux derniers échanges. Le préfixe change une fois, puis chaque étape les
  porte en moins.
- **Des schémas sans bruit** : le `title` que pydantic ajoute à chaque champ et les
  `default: null` sont retirés (12 % des schémas), et les descriptions d'action qui répétaient
  celle de leur outil sont raccourcies.
- **La marche à suivre des réglages vient avec eux** : le format d'un contact, les accords, les
  secrets sont rendus par `configure_show`, que la section demande d'appeler d'abord, au lieu
  d'être dans chaque message système.

Les plafonds de `tests/test_prompt_budget.py` descendent d'autant. Non retenu : charger les
outils à la demande, qui romprait le registre fixe du contrat de cache ; raccourcir la
constitution sans évaluations pour le vérifier.

## D124 — Il voit les images qu'il récupère, et dit lui-même ce qu'il fait

*26/10/2026 · acceptée*

- **Voir ce qu'il envoie.** Une image téléchargée sur un site ou une capture de son navigateur
  était gardée et envoyée, mais jamais vue : `attachment_look` ne connaissait que les photos du
  propriétaire. Il regarde maintenant aussi les fichiers d'Orion (`fi_…`), avec la confiance de
  leur origine (la photo d'une annonce reste du contenu web), et le résultat d'un
  téléchargement d'image ou d'une capture le lui rappelle. Il ne regarde que quand ça sert : pas
  d'appel de vision pour chaque image.
- **Un mot d'attente écrit pour le moment.** « Je m'en occupe, ça prend un peu de temps… »
  revenait dès qu'un outil sans étiquette tournait plus de dix secondes. Le petit modèle (celui
  des résumés) écrit maintenant la phrase, à partir de sa demande et des appels en cours ; s'il
  échoue ou tarde plus de quatre secondes, l'ancienne phrase part. Après du contenu venu
  d'ailleurs, il ne voit que le nom des appels, jamais leurs arguments, où une page aurait pu
  glisser ses mots. Les phrases étant toutes différentes, on en limite le nombre : la première
  après quinze secondes, puis une par minute au plus, trois par tour.

## D125 — Une application à soi : une PWA, servie par le gardien

*26/10/2026 · acceptée*

Le propriétaire veut remplacer Telegram par une application Orion sur son iPhone : parler à
Orion, approuver, administrer. Sans payer les 99 $ par an d'Apple, une appli native n'a pas de
notifications et expire tous les 7 jours. On fait donc une **application web ajoutée à l'écran
d'accueil** (PWA) : notifications Web Push, plein écran, aucune expiration, Face ID par les
passkeys. Étude : `docs/research/05-application.md`.

- **Appairage** : un code à usage unique tiré sur le serveur (`orion app pair <appareil>`),
  saisi dans l'application, qui crée une passkey liée à l'iPhone (vérification de
  l'utilisateur exigée). Sans elle, l'adresse ne sert à rien.
- **La page est servie par le gardien, la conversation par le noyau**, sur un même domaine :
  un noyau compromis ne peut ni changer le code de l'application ni afficher une fausse
  question.
- **Un oui est une signature** : le gardien rédige la question et tire un défi lié à la demande
  et au choix ; l'iPhone le signe après Face ID ; le gardien vérifie. Le noyau transmet, il ne
  peut pas fabriquer d'accord.
- **Les notifications** sont chiffrées et signées par le gardien, qui garde la clé VAPID et les
  abonnements des seuls appareils appairés.
- **Un canal comme les autres** (D88) : module `app`, outbox, boîte de réception unifiée.
  Telegram peut rester allumé pendant la transition. Les proches restent sur Telegram ou
  iMessage.
- Mode réuni d'abord (un processus, comme sur Railway) ; en mode séparé, le gardien sert le bord
  de l'application (D134).

## D126 — Des pièces structurées dans les messages : des balises, pas un outil

*26/10/2026 · acceptée*

Dans l'application, un choix se fait d'un geste : réponses rapides, éléments à cocher, fiche
d'une annonce, « Rappel posé » avec Annuler. Orion les écrit avec trois balises dans sa réponse
(`<choices>`, `<choices multiple send="…">`, `<card title="…">`, `<done undo="…">`), décrites
dans une section de la constitution présente quand le module `mobile` est allumé.

- **Des balises plutôt qu'un outil** : pas d'appel de modèle en plus, rien qui change le
  registre d'outils (cache), et un message reste un message, journalisé tel qu'envoyé.
- **L'application dessine, les autres lisent** : un canal qui se déclare `rich` reçoit le
  texte tel quel ; pour les autres (Telegram, iMessage), la boîte de réception le convertit en
  texte simple (`orion.channels.markup.plain`), qui se lit bien. La notification aussi.
- **Ce qui est mal formé reste du texte**, tel qu'écrit ; un lien de carte n'est gardé qu'en
  https. Un appui envoie à Orion le texte de l'option (ou la phrase `undo`), comme s'il l'avait
  écrit : aucune action ne part sans passer par lui ni par ses accords.

## D127 — Piloter Orion depuis l'application : une vraie pause, et la console en mieux

*26/10/2026 · acceptée*

L'application montre ce que la console montre (D101), lu aux mêmes endroits, et ajoute ce qu'on
veut faire d'un geste depuis son téléphone :
- **une pause** : « 1 h » ou « jusqu'à demain ». En pause, Orion ne prend plus d'initiative ; ce
  qui attendait attend (rien n'est jeté), et les rappels qu'il a demandés passent quand même.
  C'est un réglage du propriétaire (`initiative.paused_until`), journalisé, 30 jours au plus ;
- **les heures calmes et le nombre de messages par jour**, réglés directement ;
- **sa mémoire** (chercher, filtrer, oublier ; corriger passe par Orion), **ses engagements**
  (fait, annulé), **son manuel** (retirer une règle), **son activité** et ses silences, **ce
  qu'il fera seul**, **ce qu'il coûte**.
Chaque changement est une correction du propriétaire (confiance OWNER), les mêmes événements
que la console écrit. **Reprendre une autonomie** se fait d'un geste ; **en donner une** ne se
fait pas ici : Orion la demande dans la conversation, avec ses limites et Face ID.

## D128 — Alléger seulement quand le cache est parti ; un constat se lit avec doute

*26/10/2026 · acceptée · reprise de la PR #12 (écrite le 21/10 comme D107)*

Trois points de la passation, vérifiés le 21/10 :
- **l'élagage cassait un cache encore chaud** : les vieux résultats d'outils étaient allégés
  après 10 minutes sans message, en supposant le cache du modèle expiré. C'est vrai pour Claude
  (5 minutes), pas pour DeepSeek, le modèle par défaut, qui garde le sien des heures : le tour
  suivant repayait toute la conversation pour économiser quelques jetons déjà en cache.
  L'élagage attend maintenant aussi la durée de vie du cache du modèle (`cache_minutes` :
  5 min pour Claude, 4 h pour DeepSeek, 1 h ailleurs) ;
- **`library` (keep)** : la note de mémoire d'une lecture gardée (titre, adresse, résumé,
  sujets) pouvait dépasser les 1 000 caractères d'un fait. Le document était gardé, mais la note
  refusée. L'adresse sort de la note d'abord (le document la garde), puis le résumé raccourcit ;
- **le constat mal lu** : une photo de formulaire se lit mal, quel que soit le modèle. Orion dit
  ce dont il doute et demande de quel côté est le propriétaire avant de conclure, et
  l'extracteur de mémoire ne retient pas sa lecture d'une photo tant que le propriétaire ne l'a
  pas confirmée (cas `le_constat_mal_lu`, 5/5 sur DeepSeek, 2/3 sans la consigne).
Les évals faibles l'étaient en partie à cause du juge : Haiku, par OpenRouter, réfléchit avant
de répondre et épuisait ses 400 jetons sans verdict, compté comme un non ; et il lisait « en
retard, ou coupé ? » comme « les deux ». Un juge sans verdict est maintenant signalé à part.
`il_etait_coupe`, `veille_de_partiel` et `un_lien_sans_un_mot` (qui avait besoin du module
web) passent 3/3.

## D129 — Attendre une réponse, et décider de relancer

*26/10/2026 · acceptée · reprise de la PR #12 (écrite le 21/10 comme D109)*

Orion envoie des choses qui appellent une réponse (une question à un proche, un message à un
autre Orion, un mail, une question au propriétaire), puis les oublie. Un rendez-vous de groupe
restait bloqué pour toujours par un invité muet (D99). Noan voulait qu'Orion puisse décider de
relancer, les autres comme lui. Module `awaiting`, allumé par défaut :
- **Orion décide de ce qu'il attend** : `awaiting` (wait) dit qui doit répondre (lui, un proche,
  un autre Orion, une adresse mail), à propos de quoi, et quand y revenir si rien n'arrive ;
- **une réponse clôt l'attente seule**, lue dans le journal : le message d'un proche (le
  secrétaire), n'importe quel message de cet Orion, un mail de cette adresse. Le prochain
  message du propriétaire ne la clôt pas, car il peut parler d'autre chose : au moment venu,
  Orion relit la conversation et se tait s'il a déjà eu sa réponse ;
- **au moment venu sans réponse, Orion décide** dans un tour d'initiative : relancer (avec
  `contact_message`, `peer_message` ou `mail_send` : les accords et l'autonomie s'appliquent
  comme d'habitude), le dire au propriétaire, ou laisser tomber (`SILENCE`). Une relance suit
  la précédente (`follows`) : deux au plus sur un même sujet (`max_rounds`), puis il le dit ;
- **certaines attentes s'ouvrent seules** : une demande d'accord restée sans réponse (c'était
  donc non) revient une fois, 3 h plus tard, pour lui demander s'il le veut toujours ; un
  sondage ou une proposition de rendez-vous qu'un autre Orion laisse sans réponse revient au
  bout de 24 h. Une réponse à une nouvelle demande pour la même action clôt l'ancienne ;
- c'est une alerte du moteur d'initiative : heures calmes, budget du jour, vérificateur ;
- tout se reconstruit depuis le journal : les seuls événements sont les attentes qu'Orion ouvre
  (`awaiting.opened`) et celles qu'il retire (`awaiting.cancelled`).
Les budgets du message système et des outils (D84) passent à 38 600 et 36 600 caractères. Cas
`relance_le` et `il_a_deja_repondu`, 3/3 sur DeepSeek.
Pas encore : annuler un rendez-vous de groupe qu'un invité laisse sans réponse (il n'y a pas
d'outil d'annulation pour celui qui propose) ; Orion peut seulement relancer ou prévenir.

## D130 — Laisser Orion faire seul se demande, même après un message d'un tiers (révise D90)

*26/10/2026 · acceptée · reprise de la PR #12 (écrite le 21/10 comme D111)*

Le 21/10, juste après qu'Orion lui eut relayé deux messages de Victor, Noan a écrit « discute
avec lui sans me consulter ». `autonomy_grant` (`contact_message`, Victor) a été refusé pendant
2 h : la conversation contenait le message d'un tiers, et accorder de l'autonomie faisait partie
des actions jamais faites depuis un tel contexte, même avec son oui. Le mode n'y était pour rien :
il était déjà en `equilibre`.

Désormais, en `equilibre`, `autonomy_grant` depuis une conversation qui contient du contenu venu
d'ailleurs lui est **demandé**, avec l'autonomie exacte et la raison de la question, comme toute
action sur le monde depuis D90. Elle n'est jamais prise seule, et `prudent` la refuse toujours.
Installer ou retirer un module (D105) reste refusé dans ce contexte : c'est du code qui tourne
avec sa mémoire.
Dans le cas réel, Orion n'a même pas réessayé : à la demande suivante, il a répété de mémoire le
refus d'avant (« il faut 120 minutes… »), que le message de refus lui disait de répéter tel
quel. La constitution et ce message disent maintenant qu'un refus ne vaut que pour l'appel où il
a eu lieu : s'il redemande, Orion rappelle l'outil, et c'est le contrôle qui décide. Cas
`laisse_le_ecrire_seul`, 3/3 sur DeepSeek.

## D131 — Des papiers sans types : on les retrouve en cherchant

*26/10/2026 · acceptée · révise D74*

Les papiers avaient un type pris dans une liste fixe (carte d'identité, fiche de paie, RIB…).
Le modèle ne voyait pas cette liste : `papers` find avec « assurance » était refusé, et ce qui
n'y entrait pas devenait « autre ». Le propriétaire trouve ces types inutiles ; ce qui sert,
c'est de retrouver un document.

- **Ranger** (`papers` file) : un libellé, et `about`, ce que c'est et ce qu'on y trouve, dans
  les mots qu'une recherche emploierait (« attestation d'assurance habitation MAAF,
  appartement Dreux »), jamais ses numéros. Plus de type.
- **Chercher** (`papers` find) : par les mots (sans accents ni majuscules) et par le sens
  (les vecteurs de la mémoire), fusionnés par rang comme dans la bibliothèque. Sans mots :
  tous, du plus récent au plus ancien.
- **Le dossier** : Orion choisit les papiers et leur ordre, et dit ce qui manque (`missing`) ;
  l'ordre qu'attend un bailleur est dans la consigne, plus dans le code.
- **L'expiration** : tout papier qui a une fin de validité est signalé trois mois puis un mois
  avant. Avant un voyage, Orion voit ses papiers qui ont une date de fin, valides ou non le
  jour du départ, et juge lui-même ceux qu'il lui faut.
- Les papiers rangés avant gardent leur ancien type, lu comme ce qu'ils sont (`about`).

## D132 — Ce que le modèle lit de ses outils dit vrai

*26/10/2026 · acceptée*

Une passe sur les 65 outils, et sur les 178 appels d'une évaluation, le 26/10 :

- **Les outils à actions montrent les types** : chaque champ d'un outil groupé (`mission`,
  `playbook`, `relations`…) était déclaré sans type ni valeurs permises. Le modèle ne savait
  pas qu'un mois est un nombre, ni que `playbook` (learn) n'accepte que quatre portées : deux
  appels sur 178 ont échoué ainsi, et chaque échec coûte une étape. Le schéma montre maintenant
  le type et les valeurs permises de chaque champ (pas les longueurs : le modèle de l'action
  les vérifie et dit ce qui ne va pas). Un champ que deux actions typent différemment reste
  sans type. Le budget des outils (D84) passe à 37 600 caractères.
- **Les descriptions suivent D115** : `browser` (open, type), `web_read` et `file` (download)
  disaient encore « seulement les adresses qu'il a données » et « les formulaires sont
  bloqués », ce qui n'est plus vrai depuis D115 et pouvait le faire renoncer.
- **Petites erreurs** : un suivi (`follow_up`) prend l'identifiant `fu_…` (il partageait `f_…`
  avec les faits) ; `autonomy_grant` n'a plus de champ `domains` (rien ne demande plus d'accord
  pour le navigateur depuis D115) ; `hand_read_file` renvoie au bon nom d'outil.

## D133 — Le travail en direct dans l'application, et l'arrêter

*26/10/2026 · acceptée*

Quand Orion travaille sur son message, l'application montre une carte à la place de « … » :
ce qu'il fait (le `doing` de chaque outil, dans ses mots), les étapes déjà faites, le temps
écoulé, ce que le tour a coûté, et **Arrêter**.
- La boucle annonce les outils qu'elle va lancer (`Working`) ; un canal qui montre le travail
  (`LiveOutlet` : `watch`, `stopped`) le reçoit par la boîte de réception. Rien n'est journalisé
  en plus : la fin du tour l'est déjà (`turn.finished`).
- **Arrêter** s'applique entre deux étapes (`halted`, comme pour une tâche annulée) : un outil
  déjà lancé finit, le suivant ne part pas, et Orion répond « D'accord, j'arrête là. ».
- **En fond** envoie, en clair dans la conversation, « Passe ça en tâche de fond et
  préviens-moi quand c'est prêt. » : un message arrivé pendant un tour le rejoint entre deux
  étapes, et Orion confie la suite à une tâche de fond (D110) avec tout ce qu'il sait.

« Pour toi » (la cloche) rassemble ce qui l'attend et ce qu'Orion a en cours : les accords, les
questions qu'Orion attend de lui (D129), ses tâches de fond (D110, avec leur dernier compte rendu,
et Arrêter), et les réponses qu'il attend des autres (Laisser tomber). La pastille compte les
accords et les questions. Arrêter une tâche ou laisser tomber une attente est journalisé comme
venant de lui.

## D134 — En mode séparé, le gardien tient le bord de l'application

*26/10/2026 · acceptée · complète D125*

Sur un serveur, l'installation sépare le noyau et le gardien (D28). L'application ne pouvait pas
s'y charger : un noyau compromis qui sert les pages, les passkeys et les questions pourrait
afficher une action et en faire signer une autre. Le gardien tient donc maintenant le bord :
- **il sert l'application elle-même** (ses fichiers, figés dans le paquet), l'appairage, la
  connexion, les sessions (signées de sa clé), les accords et les abonnements aux
  notifications, sur `[modules.mobile] port` ;
- **les codes d'appairage ne viennent que de son utilisateur** (`orion-admin app pair iphone`,
  avec son jeton d'administration) ; le noyau ne peut ni en tirer un ni couper un appareil ;
- **les accords** : il rédige la question, l'application la lit chez lui, la signature de cet
  accord et de cette réponse lui parvient, et seule une réponse qu'il a vérifiée laisse passer
  l'action (`_owner_said_yes`) ; un « non » sans Face ID ne vaut jamais oui. Le noyau attend,
  lui demande la réponse, et présente l'accord avec l'action, comme sur Telegram ;
- **les notifications** : la clé VAPID, les abonnements, la limite de débit ;
- **le noyau ne sert que la conversation**, `/app/api/`, sur `core_port`, en demandant au
  gardien à qui est chaque session (gardé 30 secondes ; injoignable, c'est non).

En mode réuni (Railway, un poste), le même processus sert tout, sur les deux ports, pour que
les mêmes règles de Caddy marchent.

## D135 — L'application en quatre onglets, et le serveur réglé depuis le téléphone

*26/10/2026 · acceptée · étend D125, D127 et D92*

L'application n'était qu'une conversation, avec un panneau Contrôle et un panneau Pour toi
derrière deux boutons. Il fallait encore le serveur pour lire `orion.toml`, allumer un module ou
appairer un deuxième téléphone. Désormais :
- **quatre onglets**, dans une barre flottante en verre (style iOS 26), avec la recherche dans
  ce qu'Orion sait dans un rond à part :
  - **Maintenant** : ce qu'Orion fait, puis ce qui attend le propriétaire (les accords à
    glisser, les questions), les tâches de fond, ce qu'Orion attend des autres et ce qu'il fera
    bientôt. Le badge compte ce qui l'attend ;
  - **Conversation** : comme avant ;
  - **Carnet** : la mémoire, les engagements, le manuel, l'activité, ce qui est prévu, ce
    qu'Orion fait seul ;
  - **Réglages** : la pause, les coûts, les heures calmes, puis le serveur et les téléphones ;
- **le serveur, en lecture** (`GET /app/api/server`) : la version, chaque réglage
  d'`orion.toml` qui peut changer par message avec le niveau de oui qu'il demande (D92), les
  modules allumés ou éteints et le catalogue (D105). Les secrets et les chemins n'y figurent
  pas ;
- **un changement se demande dans la conversation**. Toucher un réglage, un module ou
  « Installer » envoie à Orion la demande exacte (« Dans orion.toml, mets `x` à `y` »). Orion la
  vérifie en entier, et le propriétaire la signe avec Face ID, comme par message. L'application
  n'écrit donc jamais le fichier : c'est le même chemin, vérifié par le gardien ;
- **les téléphones** (`/app/devices`, au bord, donc au gardien en mode séparé) : la liste ;
  un nouveau code d'appairage, ou un téléphone retiré, chacun signé avec Face ID pour ce
  changement seul (`pair:<nom>`, `revoke:<nom>`). Le téléphone en main ne se retire pas lui-même :
  il se déconnecte.
- Orion parle de lui-même à la première personne partout dans l'application.

- **les missions** (D47), dans le Carnet et dans Maintenant : le but, les critères, le rythme,
  le dernier passage, ce qu'Orion a retenu et ce qui est écarté avec son motif. Depuis l'app, le
  propriétaire demande un passage tout de suite, change le rythme, écarte un élément avec son
  motif ou arrête la mission. Chaque geste est journalisé comme venant de lui. Le dernier
  passage est marqué quand il repose sur des pages lues ;
- **les machines** (D1) : lesquelles sont en ligne et ce que chacune permet, et en couper une.
  En appairer une reste sur le serveur (`orion hands pair`) : ce qu'une machine renvoie compte
  comme venant du propriétaire, donc une session volée ne doit pas pouvoir en ajouter ;
- **la version** : le commit qui tourne, sa date, et une mise à jour écartée par le serveur
  (`update.bad`, D94). Il n'y a rien à lancer : le serveur regarde toutes les deux minutes et
  revient en arrière si ça casse.
- **le reste du carnet** (`orion.mobile.notebook`) :
  - les papiers (D131), à chercher et à ouvrir, avec ceux qui expirent ;
  - les proches, avec ce qu'Orion sait d'eux, leur anniversaire, leur rythme et « Vu
    aujourd'hui », journalisé comme venant du propriétaire ;
  - les pages de ses semaines (D52) ;
- **les points du jour** : leurs heures se règlent dans Réglages, jusqu'à six ;
- **prendre la main sur le navigateur** (D116) : une passation apparaît dans Maintenant, avec
  l'onglet en direct (toucher pour cliquer, taper dessous, « J'ai fini »). Elle marche aussi
  sans Telegram, l'app suffit. Ce qu'il tape va dans la page, jamais dans le journal ni les
  logs ;
- **le mode voix** : un rond pour parler, et les réponses d'Orion lues à voix haute (sa propre
  voix s'il en envoie une, sinon la synthèse de l'iPhone).

Constaté en vrai le jour même :
- **un réglage demandé depuis l'app était refusé**. En mode `prudent`, `configure_change` ne
  part jamais d'une conversation qui contient une page ou un mail, et celle de l'app en contient
  presque toujours. Un changement fait dans Réglages ne passe donc plus par le modèle : le
  propriétaire signe avec Face ID **ces changements exacts** (`config:<empreinte>`), le bord
  vérifie la signature et écrit le fichier, vérifié en entier, sous sa trace (`signed_by`).
  Le bord, c'est le gardien en mode séparé, le module `configure` dans un seul processus. Le
  cœur voit le fichier changer et redémarre (`configure.watch`), comme après une modification
  faite à la main sur le serveur. Les secrets et les chemins restent refusés, comme par
  message. Installer ou retirer un module passe encore par la conversation : c'est le cœur
  qui l'installe ;
- **« Mes proches » montrait le propriétaire, des villes et son assureur**. Les noms de
  `about` ne sont pas tous des gens. N'y figure plus que quelqu'un dont la mémoire parle comme
  d'une personne, ou qu'Orion suit (anniversaire, rythme, rencontre), et jamais le
  propriétaire ;
- **le haut et le bas de l'écran** : un bandeau sous la barre d'état, des halos trop forts
  derrière des boutons blancs, et un clavier qui décalait l'écran. Les halos sont plus doux
  et ne montent plus jusqu'en haut. Les bords flous remplacent les bandes. Les boutons sont
  teintés, les listes ont la forme des Réglages d'iOS, avec leurs icônes de couleur. L'app
  suit la zone visible quand le clavier monte.

## D136 — L'application : moins de place pour le décor, plus pour ce qu'on fait

*26/10/2026 · acceptée · révise la mise en page de D135*

Le propriétaire trouvait l'interface peu pratique. Regardée à la taille d'un iPhone, avec des
données réalistes :
- **Maintenant** : le titre, un grand rond et une phrase en 28 px prenaient la moitié de
  l'écran ; un accord à signer commençait au milieu et son curseur était caché sous la barre
  d'onglets. L'état d'Orion tient maintenant dans une carte (le rond, une ligne, ce qui vient
  ensuite, ses boutons), et « elles sont juste en dessous » a disparu ;
- **un accord dit l'action en mots** (« Envoyer un mail », pas « MAIL SEND ») ; un outil
  inconnu garde son nom. Un texte long se replie plus tôt (120 caractères), on le touche pour
  le lire en entier ;
- **la conversation** : un accord arrivé pendant que l'app était fermée restait sous le bas de
  l'écran ; on y descend maintenant si on était en bas. Le bouton du mode voix a son icône
  (une onde), il ne ressemble plus au micro du message vocal ;
- **Carnet** : une mosaïque de tuiles de trois formes, deux icônes identiques, un « ? » pour
  les proches. Ce sont maintenant trois listes courtes, comme les Réglages d'iOS (ce que je
  fais, ce que je sais, ce que tu m'as confié), avec la recherche en haut ;
- **Réglages** : le point vert débordait de sa carte, « 22:30–07:30 » tenait sur deux lignes,
  et une tuile entière servait à un compteur. L'état et la pause forment une carte, les coûts
  restent deux tuiles, et quand Orion écrit (messages par jour, heures calmes, points du jour)
  est une liste, le compteur dans sa ligne ;
- les icônes de couleur des listes restaient grises (une règle plus précise les écrasait).
Rien ne change côté serveur ni dans ce qui est journalisé.

## D137 — Une application sobre, comme celle de ChatGPT

*26/10/2026 · acceptée · révise les couleurs de D125 et D135*

Le propriétaire trouvait l'application trop colorée (lavande, halos, dégradés, une icône de
couleur par ligne) et voulait quelque chose de sobre, dans le style de l'application ChatGPT :
- **fond blanc, encre noire, surfaces gris clair** cernées d'un filet, sans ombre ni halo ni
  dégradé ; le bouton qui compte est noir ;
- **la conversation** : Orion écrit sur la page, sans bulle ; le propriétaire dans une bulle
  grise ;
- **les icônes** sont des pictogrammes gris, sans pastille de couleur ; l'onglet choisi est en
  noir sur gris ;
- **la couleur ne reste que là où elle dit quelque chose** : le rouge d'une action qui défait,
  le point vert « en ligne » ou « actif » ;
- des titres un peu plus petits, des étiquettes de section en minuscules.
Seules les couleurs changent (et le manifeste, blanc) ; la mise en page reste celle de D136.

## D138 — L'application en cobalt : du relief, et une couleur pour Orion

*26/10/2026 · acceptée · révise les couleurs de D137*

Sur quatre pistes tirées de l'application de D137 (affinée, cobalt, douce, contrastée), le
propriétaire a choisi le cobalt :
- **du relief** : un fond gris clair comme dans iOS, des surfaces blanches avec une ombre légère
  au lieu du filet, un seul arrondi, des titres un peu plus grands ;
- **une couleur à Orion**, un bleu cobalt (`#2f5bff`), pour ce qu'il fait et ce que le
  propriétaire choisit : son signe, le bouton qui compte, la glissière d'accord, l'onglet choisi,
  les réponses rapides, les jauges, et les bulles du propriétaire ;
- le reste ne change pas : pictogrammes noirs sans pastille, rouge pour ce qui défait, pastille
  rouge pour ce qui attend.
- **du mouvement**, sur des ressorts comme dans iOS, une seule animation par changement :
  l'onglet choisi glisse sous une lentille, l'onglet arrive du côté de son bouton, le contenu
  d'une page arrive en cascade, un nouveau message monte à sa place, les panneaux poussent
  depuis la droite et les feuilles montent et redescendent ; ce qui se touche s'enfonce sous
  le doigt, la glissière d'accord efface ses mots en avançant et finit sur une coche.
C'est une couche à la fin de `app.css`, et quelques classes posées par `app.js` ; la mise en
page reste celle de D136.

## D139 — L'application adopte Messages et les gestes d'iOS 26

*27/10/2026 · acceptée · révise D137 et D138*

L'application reprend désormais Messages sur iPhone, en mode clair. Une seule feuille de
styles remplace les couches précédentes ; les appels au serveur et les actions restent les mêmes.

- **La conversation** : bulles bleues envoyées et grises reçues, regroupées sur deux minutes,
  avec une queue à la fin du groupe ; séparateurs de date et d'heure, état de distribution,
  heures révélées en glissant vers la gauche et bulle à trois points quand Orion écrit.
- **Les tapbacks** : un appui long soulève le message au-dessus d'un fond flouté, avec les
  réactions en capsule et les actions en dessous ; la réaction choisie rejoint sa bulle.
- **Le bleu système iOS** (`#007AFF`) remplace le cobalt. Maintenant, Carnet et Réglages
  reprennent les listes groupées des Réglages, leurs icônes de couleur et leurs interrupteurs.
- **Le verre d'iOS 26** habille les commandes, la barre d'onglets flottante et les feuilles ;
  les panneaux poussent depuis la droite et les grands titres se replient au défilement.
- **Le mouvement** : ressorts à l'envoi et à la réception, indicateurs de travail, retour
  d'appui, coches et badges animés, actualisation en tirant et retour au dernier message.
  La préférence de mouvement réduit désactive ces animations.

## D140 — Orion et son application, mieux joints

*27/10/2026 · acceptée · complète D125, D126, D133*

L'application montrait la conversation et quelques écrans, mais Orion en savait peu sur elle,
les notifications menaient au dernier onglet ouvert, rien ne bougeait hors des messages, et une
bonne part de ce qu'Orion suit pour le propriétaire n'y avait pas d'écran. Quatre ajouts :

- **Orion connaît son application** (`app.md` réécrit) : les quatre onglets, sa carte de
  travail avec Arrêter et En fond, la pause, les notifications, les réglages signés avec
  Face ID, et la liste exacte des pièces. L'analyseur applique les limites que le texte donne
  (deux à cinq réponses rapides, huit options à cocher, trois cartes, huit pièces par message ;
  au-delà, la balise reste du texte).
- **Quatre pièces de plus**, dessinées comme iOS : `<event>` (une carte de Calendrier, avec
  « Ajouter à l'agenda », qui le lui demande dans la conversation : l'ajout reste son outil et
  son accord), `<mail>` (l'expéditeur, l'objet et ce qu'Orion en retient, jamais le mail),
  `<open screen>` (un bouton vers un écran de l'app, sur une liste fermée) et `<progress>`.
  Ailleurs qu'à l'app, elles deviennent du texte ; `<open>` disparaît.
- **Des notifications qui mènent quelque part** : chacune dit ce qu'elle concerne (`message`
  ou `approval`) et un appui ouvre ce message, surligné, ou cette demande dans Maintenant, que
  l'app soit fermée ou déjà ouverte. Là où le système montre des actions (pas iOS), on peut
  « Refuser » une demande depuis la notification ; dire oui passe toujours par l'app et Face ID.
- **L'app suit ce qui change** : un service suit le journal et dit à l'app ouverte quelle
  partie a changé (tâches, missions, attentes, agenda, mails, nuit, carnet, réglages, dépenses),
  jamais le contenu ; une rafale fait une seule mise à jour, les dépenses suivent avec cinq
  secondes de retard. L'onglet ou le panneau visible se repeint en fondu.
- **Ce qu'Orion suit pour lui a ses écrans**, en lecture : son agenda (deux semaines, et
  « Aujourd'hui » dans Maintenant), ses mails qui comptent (le résumé d'Orion, les échéances,
  les réponses attendues), et les nuits d'Orion (D117 : ce que chacune a lu et coûté contre son
  budget, ses notes ; la note du matin en tête de Maintenant jusqu'à midi). La nuit enregistre
  désormais son coût.

Pas encore : pas de notification de plus pour une tâche, une mission ou la nuit finies,
puisqu'Orion en écrit déjà un message (qui, lui, est notifié) ; pas de réponse écrite depuis la
notification (le web ne le permet pas) : « Répondre » ouvre la conversation, prête à écrire.

## D141 — Avec l'application, plus rien en double sur Telegram

*27/10/2026 · acceptée · complète D88, D116 et D125*

Le propriétaire, passé à l'application, recevait encore certains messages sur Telegram en
plus. Deux chemins contournaient la boîte de réception unifiée, qui ne choisit qu'un seul
canal :
- **le gardien** prévenait que le cœur ne répondait plus, puis qu'il était reparti, par
  iMessage ou Telegram seulement. Il passe d'abord par la notification de l'application si un
  téléphone l'accepte (le texte entier, le cœur ne pouvant pas l'écrire dans la conversation),
  et sinon comme avant ;
- **la reprise du navigateur** envoyait un bouton Telegram en plus de « Prendre la main » dans
  l'application. Avec l'application, le bouton ne part que s'il a écrit en dernier depuis
  Telegram.

Reste voulu (D88) : un message proactif va là où il a écrit en dernier. S'il écrit sur
Telegram, la suite y va jusqu'à son prochain message dans l'application. Garder un
identifiant (`browser_save_password`) reste sur Telegram : l'application n'a pas encore ce
formulaire.

## D142 — Le coffre dans l'application, rempli par les mots de passe du téléphone

*27/10/2026 · acceptée · complète D111, D116, D120 et D135*

Le coffre (D111, D120) ne se remplissait que par Telegram, ou sur le serveur. Et pour se
connecter dans une passation (D116), il fallait toucher une image puis taper son mot de passe à
la main dans un champ « Texte à taper » : le propriétaire trouvait ça peu pratique. Désormais :
- **Mots de passe**, dans Réglages : la liste des accès gardés (le site, l'identifiant, la
  double authentification), en ajouter un, changer un mot de passe, en retirer un. Un mot de
  passe ne se réaffiche jamais. Ces routes sont au bord de l'application (`/app/logins`) : en
  mode séparé, c'est le gardien qui les sert et qui garde le coffre, et le cœur ne voit toujours
  pas le mot de passe. Il suffit d'être connecté à l'app, sans Face ID : ajouter un accès ne
  donne rien à personne, et l'iPhone a déjà demandé Face ID pour le remplir.
- **Un vrai formulaire** : les champs portent `autocomplete="username"` et `current-password`,
  donc l'iPhone propose ses mots de passe enregistrés (Face ID), au lieu de les retaper.
- **Orion demande dans l'application** (`browser` save_password) : une carte dans Maintenant
  ouvre ce formulaire, le site déjà rempli. L'app le donne au coffre, puis dit au cœur le nom
  sous lequel il est gardé ; le cœur vérifie qu'il y est, pour ce site, avant de prévenir Orion.
  Sur Telegram (s'il y a écrit en dernier, D141), comme avant.
- **Dans une passation, la page de connexion devient un formulaire** : quand la page montre un
  champ mot de passe (ou un premier champ identifiant seul), « Prendre la main » affiche
  Identifiant et Mot de passe au-dessus de l'image. Le navigateur d'Orion les tape dans les
  champs de la page et envoie le formulaire ; le mot de passe est masqué pour Orion aussitôt.
  Toucher l'image reste possible pour le reste (un code, un captcha).
- **« Garder cet accès ? » arrive tout de suite** : après « J'ai fini », si la connexion est
  passée, l'app le demande sur place, et la carte reste dans Maintenant un quart d'heure.
- Orion préfère save_password à hand_over pour une connexion simple : une fois gardé, il se
  connecte seul les fois suivantes.

## D143 — Orion au travail, en direct et sans bruit ; sa main dans le navigateur, comme Safari

*27/10/2026 · acceptée · révise D121 et D133 pour l'application, révise D116 et D142*

Le propriétaire trouvait les annonces d'Orion pendant un long tour bonnes sur le papier, mais
mal faites : chacune était un message à part, notifié, qui restait dans la conversation. La carte
« Je travaille » (minuteur, coût, étapes, deux boutons) faisait tableau de bord. Et sous la vue
du navigateur, une rangée de boutons (⏎ ⌫ ▲ ▼ ←) n'avait rien de naturel.

- **Dans l'application, ce qu'Orion dit en travaillant n'est plus un message** : la boîte de
  réception le passe au canal qui montre le travail (`LiveOutlet.narrate`). Rien n'est notifié
  ni empilé. Ailleurs (Telegram, iMessage), rien ne change.
- **Une seule bulle vivante, là où viendra sa réponse** : ce qu'il fait maintenant, en texte
  qui scintille ; la dernière chose qu'il en a dite ; son plan en liste à cocher s'il en tient
  un (`plan`, transmis par `Activity.plan` plutôt qu'en texte) ; le temps écoulé. Une réponse
  rapide garde les trois points. Arrêter reste le bouton de la barre de saisie ; « Continuer en
  fond » n'apparaît qu'après 25 s.
- **La réponse garde la trace du travail**, repliée au-dessus d'elle (« Travaillé 1 min 12 ·
  6 étapes ») : ce qu'il a dit, fait, et son plan final. Elle est enregistrée avec le message
  dans le journal de l'application ; le modèle ne la voit pas, le cache de prompt est intact.
- **La vue du navigateur occupe l'écran** : Annuler, le site et Terminé en haut ; un toucher
  clique, un glissé fait défiler ; en bas, une barre comme celle de Safari (page précédente,
  le champ où écrire, champ suivant). Toucher un champ de la page adapte ce champ : un mot de
  passe y devient un champ mot de passe que l'iPhone sait remplir. Entrée tape le texte et
  valide ; effacer dans un champ vide efface dans la page. « Se connecter avec mes
  identifiants » (D142) flotte au-dessus quand la page demande une connexion.

Corrigé au passage : orion.toml ne s'écrivait pas depuis l'application sur le serveur. Le
fichier temporaire et la copie précédente étaient écrits à côté du lien `/srv/orion/orion.toml`,
dans un dossier où systemd interdit d'écrire ; ils le sont maintenant à côté du vrai fichier
(`config/`), et le lien reste un lien.

## D144 — Orion revu : sa voix, son rythme, ce qu'il sait de lui, aller au bout (révise D15, D20, D36)

*27/10/2026 · acceptée, son rythme révisé par D146*

Le propriétaire trouvait à redire sur les quatre fronts à la fois : le ton (trop assistant, des
blagues qui tombent à plat, trop long), les prises d'initiative (trop, au mauvais moment, pour
rien), une mémoire qui ne fait pas connaître (il redemande ce qu'il sait), et des réponses qui
s'arrêtent en route. Une refonte d'ensemble plutôt que quatre rustines.

- **Sa voix** (révise D15). La constitution montre au lieu de décrire : deux exemples « non /
  oui » d'après de vrais travers. Il se cale sur la longueur et le registre du propriétaire, dit
  le résultat plutôt que son travail, a un avis. L'humour n'est plus une consigne (« une pointe
  de sarcasme ») mais une permission, quand il vient tout seul ; dans le doute, pas de blague.
  Les messages au fil du travail : seulement quand ils servent, une phrase. Une courte relecture
  (`closing.md`) ferme le message système, là où un modèle pèse le plus les consignes : la
  première phrase répond, c'est à sa mesure, ce qu'il sait de lui s'y sent, ce qui est promis
  est programmé. Budget du message système relevé à 42,2 k (sections « intentions » et
  « conseils » resserrées).
- **Aller au bout, vérifié dans le code** (étend D118). `orion.cognition.finish` relit la réponse
  finale avant qu'elle parte. Elle revient une fois au modèle quand elle finit sur une offre ;
  quand elle promet une suite (« je te tiens au courant », « je reviens vers toi ») sans qu'aucun
  outil du tour ne la programme (rappel, tâche, mission, suivi, attente…), puisqu'une fois la
  réponse partie rien ne revient ; ou quand elle a des formules d'assistant (« Bien sûr ! »,
  « N'hésite pas… ») ou des titres dans un message court. Une note, un seul retour par tour.
- **Son rythme** (révise D20, D36). Une vingtaine de sources peuvent le faire parler, et
  l'arbitre laissait presque tout passer : seuil bas, et une attente qui rendait tout urgent au
  bout d'une heure ou deux. Désormais, ce qu'Orion dit de lui-même ne part seul que si ça ne peut
  pas attendre (importance d'au moins 0,75 : un rendez-vous qui approche, une reprise en main du
  navigateur) ou que ce serait périmé au prochain point. Le reste attend le prochain point, le
  matin ou le soir, et part dans ce seul message, du plus utile au moins utile. Par défaut, deux
  points (8 h 30, 19 h) au lieu de trois, et six messages de lui-même par jour au plus au lieu
  de douze. Ce qu'on lui a écrit (un proche, une tâche de fond) et ce qu'il a demandé (rappels,
  comptes rendus) passent comme avant. Un genre de message auquel le propriétaire n'a pas
  répondu (message ou tapback dans les trois heures) les quatre dernières fois en un mois cesse
  de partir. Il apparaît dans « ce que j'ai failli te dire » le dimanche, où il peut dire que
  c'était une erreur, et revient de lui-même au bout d'un mois. Les papiers qui expirent dans un
  mois attendent un point (0,6).
- **Ce qu'il sait de lui : un portrait** (étend D27). Une liste de faits ne fait pas connaître
  quelqu'un. Chaque nuit, après le rangement de la mémoire, le modèle principal écrit un portrait
  de 1 600 caractères au plus (`orion.memory.portrait`). Il le tire de ce qui est sûr : les faits
  (pas ceux d'un mail ou d'une page), ses engagements, son manuel, ses messages des sept
  derniers jours. Le portrait dit qui il est, ce qu'il vit en ce moment, les gens qui comptent.
  Il ouvre le bloc `<memoire>` de chaque nouvelle fenêtre de conversation. C'est un événement du
  journal (`memory.portrait.written`), donc il se reconstruit comme le reste ; oublier une source
  le fait réécrire la nuit suivante. La constitution lui demande de s'en servir sans l'annoncer,
  et de vérifier sa mémoire avant toute question sur la vie du propriétaire : ce qui date se
  confirme, ça ne se redemande pas.

  Il le lit aussi en tête de Carnet › Souvenirs (« Comment je te vois »), avec « Ce n'est pas
  tout à fait ça » pour le corriger dans la conversation.

Évals ajoutées : `meteo_en_deux_phrases`, `ce_quil_sait_de_lea`, `surveiller_cest_programmer`
(2/2, 3/3, 2/2 sur `anthropic/claude-haiku-5.5`) ; les garde-fous `salut`, `un_truc_avec_paul`,
`se_lance_seul_sur_un_but`, `va_au_bout_au_lieu_de_proposer` et `quels_outils` passent toujours.

## D145 — Un navigateur qu'on ne prend plus pour un robot, et la sortie par sa connexion

*27/10/2026 · acceptée*

Orion restait souvent bloqué sur les sites : « Un instant… nous devons vérifier que vous êtes
humain » (Doctolib), « Accès temporairement restreint » (Leboncoin). Deux causes, mesurées ici :

- **Le navigateur se dénonçait.** Le Chromium « headless shell » de Playwright se présente comme
  `HeadlessChrome`, avec `navigator.webdriver` vrai, sans plugins, une seule langue, les
  notifications refusées d'office, une carte graphique logicielle. Désormais (`orion.computer.
  disguise`), c'est le Chromium complet dans son nouveau mode sans écran, sans les drapeaux
  d'automatisation. Chaque page reçoit la même histoire cohérente, celle d'un Chrome sur un
  téléphone Android, de la version réelle du moteur : identifiant et Client Hints (`Sec-CH-UA`)
  par `Emulation.setUserAgentOverride`, langues françaises, notifications « à demander »,
  carte graphique et mémoire d'un téléphone. Le texte est tapé touche par touche plutôt que
  collé d'un coup. Mesuré : tous les contrôles de bot.sannysoft.com passent ; Doctolib, qui
  bloquait l'ancien navigateur, s'ouvre ; la page de test Cloudflare nowsecure.nl passe.
- **L'adresse du serveur est refusée**, quoi que fasse le navigateur (DataDome sur Leboncoin :
  « un robot est sur le même réseau »). Le remède est d'en sortir par une connexion domestique :
  celle du propriétaire. `orion hand exit on` sur sa machine la laisse servir de sortie
  (`orion.hands.exit`). Un relais SOCKS5 sur le serveur (127.0.0.1 seulement) confie chaque
  connexion du navigateur à la machine, qui l'ouvre elle-même et renvoie les octets par une
  WebSocket qu'elle compose (`/hands/exit/<id>`). La machine refuse tout sauf les ports du web
  et toute adresse de son propre réseau (sa box, un NAS), vérifiée après sa propre résolution
  de nom, en se connectant à l'adresse vérifiée. WebRTC ne passe pas à côté du relais. Machine
  éteinte ou sortie non permise : le relais sort directement du serveur, avec les mêmes
  gardes ; le navigateur ne casse jamais pour ça.
- **Un mur se reconnaît** (`disguise.BLOCKED` : DataDome, Cloudflare, PerimeterX, Akamai, les
  captchas). La vérification de Cloudflare qui laisse passer seule est attendue (15 s). Un
  captcha qu'une personne peut passer : Orion lui passe la main (`hand_over`), le site le
  laisse ensuite passer. Un refus de l'adresse : réessayer ne sert à rien. Orion passe par une
  autre source, ou propose une fois de relier son ordinateur comme sortie.

Constaté au passage : le Chromium complet efface les cookies de session quand son dernier
onglet se ferme. Ils sont désormais gardés juste avant, et la fermeture ne les écrase plus.

## D146 — Plus d'initiative et d'intuition (révise D144)

*27/10/2026 · acceptée*

« Il manque d'initiative et d'intuition quand je l'utilise. » D144 avait lu la gêne du
propriétaire dans le mauvais sens et bridé Orion : ce qui ne pouvait pas attendre partait
seul, tout le reste attendait le point du matin ou du soir.

- **Le rythme** : ce qui a de la substance repart dès que l'arbitre le permet (importance de
  0,45 et plus : un mail important, une nouvelle d'une mission, un suivi, une veille de
  départ). Seules les petites choses attendent le prochain point : une trouvaille, une question
  pour faire connaissance, un souvenir à vérifier, un retour sur un conseil. Les trois points
  par jour reviennent (8 h 30, 13 h, 19 h), et douze messages de lui-même au plus. Un genre de
  message auquel il ne répond plus cesse toujours de partir (D144).
- **Ce qu'il voit quand on lui parle** : un bloc `<maintenant>` accompagne le message du
  propriétaire quand il a changé depuis la dernière fois. Il contient son agenda d'aujourd'hui
  et de demain, ce qui arrive à échéance dans les trois jours, ce qu'Orion suit pour lui. Les
  modules l'alimentent (`Orion.situation`). Orion peut ainsi faire les liens sans les chercher :
  « tu sors ce soir, mais tu as le dentiste à 8 h ».
- **La constitution** dit ce qu'est l'intuition : entendre ce qu'il cherche derrière sa
  demande, faire les liens avec ce qu'Orion sait, faire la suite évidente sans attendre, une
  idée de plus quand elle est concrète. Un souci ou une envie dits en passant sont une occasion
  de s'en charger. La relecture finale demande ce qu'un ami attentif aurait vu. « Sans conseils
  ni questions en plus » (D118) n'y est plus. Budget du message système : 43,4 k.

Évals ajoutées (sur `anthropic/claude-haiku-5.5`) : `voit_venir_le_dentiste` (3/4 ; le raté
programme un rappel à 7 h sans nommer le dentiste), `prend_en_charge_un_souci_en_passant` (0/4
avec la seule consigne, 4/4 avec un exemple dans la constitution), `passeport_en_passant`
(variante : il cherche dans ses papiers et donne la démarche, mais ne note pas la tâche de
lui-même : la généralisation reste partielle). `salut` est passé à 1/2 avec la première
version de l'intuition (il récitait ce qu'il savait), puis 4/4 une fois précisé qu'un salut
appelle un salut. La relecture finale renvoie aussi « je te programme un rappel » quand aucun
rappel n'est posé, et une dernière phrase qui commence par « Je peux… », même sans point
d'interrogation.

## D147 — Sur un forfait, le travail de fond suit le modèle principal

*27/10/2026 · acceptée*

Le propriétaire avait mis `model = "chatgpt:…"`. Ses conversations passaient bien par son
forfait, mais OpenRouter continuait d'être débité quand Orion travaillait seul. Chaque rôle a son
propre modèle, et seuls ceux qu'il n'avait pas nommés restaient aux valeurs par défaut, toutes
sur OpenRouter : l'extraction de la mémoire et la réflexion de la nuit (`extractor_model`), la
curiosité, la pensée de la nuit, la chronique et l'expert (`strong_model`), le regard sur les
photos et les documents (`vision_model`).

Désormais, quand `model` est sur un forfait, ces rôles suivent le modèle principal s'il ne les a
pas nommés :
- ChatGPT (D62) : `extractor_model`, `strong_model` et `vision_model` (le forfait voit les
  images et lit les documents) ;
- Claude Code (D100) : `extractor_model` et `strong_model` (il ne prend pas d'images).

Un rôle nommé dans orion.toml garde son modèle. Restent ailleurs : ce que les forfaits ne font
pas (fabriquer une image, la voix, la transcription, les embeddings) et le vérificateur, qui
doit venir d'une autre famille que le modèle principal (`verifier_model`, un court appel par
message spontané). Les modèles de secours (`fallback_models`) ne servent que si le forfait
échoue, quota épuisé compris. Pour ne plus rien envoyer à OpenRouter, il faut nommer
`verifier_model` (un autre modèle du forfait, `orion chatgpt models`) et `fallback_models`, et
couper `embedding_model`.

## D148 — orion.toml s'écrit même après un `orion setup` lancé en root

*27/10/2026 · acceptée (complète D143)*

Après D143, l'application affichait encore « permission denied » quand le propriétaire, ou
Orion, changeait un réglage. `install.sh` lance `orion setup` en root, et celui-ci garde
l'ancienne version du fichier dans `config/orion.toml.previous`. Le script rendait ensuite au
gardien `orion.toml`, mais pas cette copie : restée à root, le gardien ne pouvait plus la
réécrire, et chaque changement échouait avant même de toucher au fichier.

Le gardien ne réécrit plus dans un fichier existant : il écrit à côté, puis remplace, pour la
copie comme pour orion.toml. Le dossier `config/` étant le sien, un fichier laissé par root est
remplacé quand même. `install.sh` rend aussi tout le dossier au gardien après `orion setup`.
Rejoué avec un utilisateur ordinaire : l'ancien code échoue sur `orion.toml.previous`, le
nouveau passe. Le serveur se répare à sa prochaine mise à jour automatique, sans rien relancer.

## D149 — Les situations : ce qui se passe dans sa vie en ce moment (chantier 1 de la refonte)

*27/10/2026 · acceptée*

Premier chantier de la refonte (`architecture/05-refonte.md`). La mémoire n'avait que des
faits, censés rester vrais. Ce qui dure un temps (un voyage, une maladie, une recherche
d'appart, un examen à préparer, un souci, un projet qui finira) devenait un fait qui ne
mourait jamais, ou n'était pas gardé du tout parce qu'il ne « vaudrait plus rien dans un mois ».
Le propriétaire le voyait : Orion garde du temporaire et rate l'important.

Une **situation** a un titre, ce qu'on en sait, les gens qu'elle touche, un horizon (la date
où elle devrait être finie, si on la connaît), des nouvelles (les six dernières), et une fin :
réglée, abandonnée, ou passée son horizon. Événements `situation.opened`, `situation.updated`,
`situation.closed` ; projection `Situations`, tenue par `MemoryService` à côté des faits.
- **Orion** les tient en conversation avec l'outil `situation` (open, update, close, list) ;
  `remember` ne garde plus que ce qui reste vrai.
- **L'extracteur** voit les situations ouvertes et sépare désormais fait durable, situation et
  engagement ; ce qui ressort d'une situation et qui dure (sa nouvelle adresse) devient un fait.
- **La nuit**, le code ferme une situation deux jours après son horizon. Un tri relit les faits
  notés depuis la dernière nuit (120 par nuit, du plus ancien au plus récent : une grande
  mémoire est triée en quelques nuits) ; ceux qui n'étaient pas durables sont clos comme faits
  et rouverts en situation s'ils durent encore. La situation garde la source du fait : oublier
  cette source l'oublie aussi.
- **`<maintenant>`** les montre d'abord (« en cours », six au plus, les dernières touchées),
  seulement celles qui viennent de lui ou d'Orion. Le **portrait** de la nuit s'écrit à partir
  d'elles et de celles qui viennent de finir. Le **Carnet** de l'application les montre sous
  son portrait, « En ce moment ».

Évaluation `un_voyage_est_une_situation` (« je pars à Lisbonne vendredi avec Léa, retour
lundi ») : 3/3 sur Haiku 5.5. Pas encore vérifié en vrai : le tri de sa mémoire réelle la
première nuit.

## D150 — La veille : Orion prend du recul dans la journée (chantier 2 de la refonte)

*27/10/2026 · acceptée*

Vingt sources pouvaient faire parler Orion (agenda, mails, missions, papiers, proches…), chacune
avec sa règle, sans voir les autres ; et Orion ne réfléchissait à la vie du propriétaire dans
son ensemble qu'une fois, la nuit, sur la conversation. Le propriétaire le trouvait sans
intuition ni recul.

Le module `watch` (actif par défaut) : trois fois par jour (7 h 30, 12 h 30, 18 h 30 ; réglable),
Orion prend un moment à lui, dans son propre fil (`veille`), avec le modèle fort et ses outils.
Il lit ses situations (D149, avec celles sans nouvelles depuis trois semaines et celles qui
viennent de finir), son agenda d'aujourd'hui et de demain, ses échéances, les 30 derniers faits
appris, ce qu'il a dit depuis la dernière veille (seulement ce qui est de confiance), et ses
propres conclusions des trois derniers jours pour ne pas se répéter. Il se demande ce qu'un ami
attentif verrait (ce qui coince, ce qui approche et se prépare, les liens, ce qui s'est tu, une
idée concrète), fait ce qui se fait d'ici (chercher, préparer, mettre à jour une situation,
poser un rappel, lancer une tâche), puis conclut : `SILENCE`, `MAINTENANT : …` ou
`AU POINT : …`. La conclusion devient une alerte du moteur d'initiative (importance 0,6 : elle
part ; 0,3 : elle attend le prochain point), avec les heures calmes, le budget du jour et le
vérificateur. Une veille manquée est rattrapée une fois ; Orion en pause, elle ne tourne pas.

Comme la nuit, rien ne part de ce fil et rien n'est demandé au propriétaire : le gardien
refuse ce qui agit sur le monde (`REFLECTIVE_THREADS`), et ce qui demande son oui va dans la
conclusion. Le module n'ajoute rien au message système : tout est dans la consigne de la veille.

Évaluations (Haiku 5.5 pour Orion, Opus pour la veille) : `la_veille_voit_le_passeport`
(un passeport qui expire le 29 et un Eurostar pour Londres le 31, appris séparément) 2/2 ; elle
relie les deux, rappelle que la carte d'identité ne suffit plus au Royaume-Uni et propose le
passeport temporaire en préfecture. La première version ne voyait pas les faits récents et
ratait le lien (« s'il n'y a pas de voyage en vue… ») : d'où les 30 derniers faits.
`la_veille_se_tait_sans_raison` 2/2 (rien ne part tout de suite). Coût : 5 à 20 centimes par
veille sur OpenRouter, rien sur un forfait. Pas encore fait : une veille déclenchée par un
événement (un mail important, un agenda qui bouge), en plus des heures fixes.

## D151 — Le tiroir : moins d'outils sous les yeux ; faire maintenant avant de remettre à plus tard (chantier 3)

*27/10/2026 · acceptée*

À chaque pas, le modèle choisissait parmi 58 outils (46 avec les modules par défaut), dont
beaucoup servent une fois par mois ou quand il le demande. Et le propriétaire a vu Orion lancer
une mission « toutes les 6 h » pour ce qui méritait une réponse tout de suite.

- **Le tiroir.** `ToolRegistry` prend une seconde liste, le tiroir (`DRAWER_TOOLS` dans
  `app.py`) : autonomie, provenance, confinement d'une source, réglages, modules, console,
  dépense, chronique, carnet d'erreurs, style, doute, mains sur ses machines… Le modèle ne voit
  qu'un outil, `orion`, qui les liste d'une ligne chacun ; il les appelle par
  `orion(tool, args)`. La boucle remplace aussitôt cet appel par celui de l'outil lui-même :
  le gardien, les accords du propriétaire (vérifiés par nom), le journal et les évaluations
  voient `autonomy_grant`, jamais `orion`. Des arguments faux renvoient le schéma de l'outil,
  que le modèle n'a pas vu, pour qu'il refasse l'appel. Avec les modules par défaut : 31 outils
  au lieu de 46, 32,7 k caractères de schémas au lieu de 40 k ; le message système perd aussi
  ses paragraphes sur l'autonomie et la provenance, resserrés (43,3 k avec tous les modules).
- **Faire maintenant d'abord**, vérifié dans le code (étend D144). La relecture finale lit
  désormais l'action de chaque appel (`mission:start`, `task:start`). Un tour du propriétaire
  qui remet sa demande à plus tard (mission, tâche de fond, réflexion pour la nuit) sans avoir
  rien cherché (web, navigateur, mémoire, papiers, mails, expert…) revient une fois au modèle :
  ce qui se trouve en quelques recherches se cherche et se répond tout de suite ; ce qui est
  lancé ne sert qu'à la suite.

Pas fait : réunir les façons de remettre à plus tard (`remind_me`, `follow_up`, `think_later`,
`awaiting`) en un seul outil. Le vrai travers était de remettre au lieu de faire, que la
vérification ci-dessus vise ; fusionner les outils obligerait à réécrire leurs événements.

## D152 — Les ratés de sa vraie vie (chantier 4)

*27/10/2026 · acceptée*

Les évaluations s'écrivaient à la main, après qu'il a raconté ce qui n'allait pas. Ses
signaux le disent déjà dans le journal : un 👎 sur un message d'Orion, et ses mots quand il
le corrige (« non, c'est pas ça », « tu t'es trompé », « je t'ai déjà dit… »).
`orion misses` les liste avec ce qu'il avait demandé et ce qu'Orion a répondu ;
`orion misses --case N` en écrit le début d'un cas d'évaluation (sa demande, le moment, le
contexte), à compléter d'une vérification et à ajouter à `evals/cases.py`. Rien ne quitte son
serveur. Lu à partir des signaux qu'Orion collecte déjà pour apprendre son manuel
(`adaptation/signals.py`), sans nouvel événement.

## D153 — Ce que son téléphone sait (chantier 6, première source)

*27/10/2026 · acceptée*

Orion ne voyait de sa vie que ses messages, son agenda, ses mails, ses papiers et ses
fichiers. Sans application native, ce que l'iPhone sait passe par les automatisations
Raccourcis, que le module `places` utilisait déjà pour les arrivées et départs (D57). Avec le
même jeton, ses automatisations envoient maintenant `{"kind", "value"}` à `/places/sense` :
son sommeil (au réveil), son mode concentration, une batterie faible, la fin d'un
entraînement, sa santé, ce qu'il écoute. Événement `phone.sensed` (confiance du téléphone, pas
ses mots). Ce qu'il a dit depuis moins de 18 h, et le dernier lieu où il est arrivé ou d'où il
est parti, apparaissent dans `<maintenant>` et dans la veille (D150). `orion places token`
explique quoi régler. Caddy fait passer tout `/places/*`. Seulement ce qu'il choisit d'envoyer,
et Orion n'en déduit rien d'autre.

Le format des messages (chantier 5) n'a pas changé : le modèle écrit déjà une fois, avec les
balises de l'application, et chaque canal les rend à sa façon (D126). Les sources suivantes du
chantier 6 (contacts, trajets, colis et abonnements, banque, maison, flux) restent à faire.

## D154 — Plus rien sur le serveur : tout passe par l'app

*27/10/2026 · acceptée*

Il voulait ne plus avoir à se connecter à son serveur. Ce qui demandait encore `orion …` ou
`orion-admin …` se fait dans l'app, onglet Réglages :

- **Ses secrets** : une clé tapée dans l'app va droit au gardien (son côté de l'app, D125) ;
  ni le cœur ni le modèle ne la voient, le registre ne garde que son nom. La signature Face ID
  porte sur le nom et l'empreinte de la valeur : une session volée ne peut ni en poser une, ni
  en changer une signée pour une autre valeur. Une clé posée ainsi remplace celle du serveur
  (avant, le coffre ne remplaçait jamais `guardian.env`). La seule clé du cœur
  (`OPENROUTER_API_KEY`) est écrite par le gardien à côté d'`orion.toml`
  (`core.secrets.env`, lisible par le groupe) ; le cœur la charge au démarrage, la surveille
  comme `orion.toml` et redémarre. `secret_ask` marche sans Telegram : la demande arrive en
  notification et dans l'app.
- **Les autres Orions** (D29) : allumer la fédération (le gardien crée la clé dans son coffre,
  puis écrit `federation.enabled`), inviter, accepter un code, délier ; chaque fois signé avec
  Face ID pour ce changement précis.
- **Une machine** (D1) : son code, signé avec Face ID pour cette machine. La signature est
  vérifiée par le gardien, qui ne signe pour le cœur que des buts commençant par `core:` : rien
  de ce que le cœur demande ne peut valoir un accord ou un changement de réglage.
- **ChatGPT** (D62) : la connexion (l'adresse à ouvrir, puis celle où le navigateur revient),
  l'état, la déconnexion. Orion démarre même s'il n'est pas encore connecté, et relit ses
  jetons quand le fichier change : pas de redémarrage.
- Sans signature, comme les mots de passe un par un (D142) : l'**import d'un export CSV** de
  ses mots de passe, le **jeton du téléphone** (D153), ses **notes** dans la bibliothèque, et
  **ses ratés** (D152).

Le code du premier téléphone s'affiche à la fin d'`install.sh`. Les commandes restent pour
dépanner, et pour ce qui tourne ailleurs que sur le serveur (`orion hand` sur sa machine).
Reste sur le serveur : la connexion Google Agenda en écriture (`orion google auth`), à faire
une fois, et `orion inkbox setup`, qui fait partie de l'installation.


## D155 — La veille se réveille quand sa vie bouge (complète D150)

*27/10/2026 · acceptée*

La veille ne tournait qu'à heures fixes : un rendez-vous déplacé à 9 h n'était regardé dans
son ensemble qu'à 12 h 30. Désormais, ce qui bouge dans sa vie déclenche une veille entre les
heures fixes :
- un **mail** qu'Orion a jugé bon de lui dire (l'alerte `mail:` est partie : c'est lui qui a
  lu le mail et l'a trouvé important, D114) ;
- son **agenda** dans les 48 h : un rendez-vous ajouté, déplacé (« Dentiste a bougé : jeu 10:00
  → jeu 14:00 »), disparu, ou une invitation. La première lecture d'un agenda n'est pas une
  nouvelle ; ce qu'Orion a écrit lui-même non plus.

La projection de la veille les suit à partir du journal, sans nouvel événement de source.
Garde-fous : trois par jour au plus (`stirred_per_day`, 0 pour n'en garder aucune), deux heures
au moins après la veille précédente (`stirred_gap_minutes`), et rien si une veille fixe arrive
dans l'heure : elle le verra. Chaque veille, fixe ou déclenchée, lit ce qui a bougé depuis la
précédente (`<ce-qui-a-bouge>`) ; une veille déclenchée sait qu'elle l'est et regarde d'abord
ce que ça change pour le reste. `watch.done` garde son heure, ce qu'elle a vu et si elle a été
déclenchée.

## D156 — Le relevé de la semaine (chantier 4, complète D152)

*27/10/2026 · acceptée*

`orion misses` liste les ratés un par un. Pour choisir le chantier suivant, il fallait aussi la
semaine d'un coup d'œil. `orion week` (et `GET /api/week`, pour l'écran Serveur de l'app,
D154) relit les sept derniers jours du journal, à côté des sept d'avant :
- les **tours arrêtés avant la fin**, par raison (à court de pas, de budget, tournait en rond,
  réponse coupée, modèle muet, outil planté), et combien en lui répondant ;
- les **outils en erreur**, les plus fréquents d'abord (les refus du gardien comptent) ;
- les **réponses corrigées** : ses 👎 et ses corrections (D152), les dernières en clair ;
- le **coût**, dont ce qu'Orion a dépensé sans qu'il ait rien demandé, et les fils qui coûtent
  le plus (`veille`, `nuit`…).

Rien de nouveau n'est enregistré : `turn.finished`, `tool.executed`, `spend.recorded` et les
signaux d'adaptation suffisent. Rien ne quitte son serveur.

## D159 — Son carnet d'adresses (chantier 6, deuxième source)

*27/10/2026 · acceptée*

Orion ne connaissait les gens de sa vie que par ce qu'il en disait. Son téléphone les connaît
tous : leurs numéros, leurs adresses, leurs anniversaires. Il exporte ses contacts en un
fichier `.vcf` (iPhone : Contacts, tout sélectionner, partager ; Android : Contacts, exporter)
et le donne à Orion depuis l'app (`POST /api/contacts/import`, Réglages) ou avec
`orion contacts import <fichier>`.

Le module `contacts` (actif par défaut, vide tant qu'il n'a rien importé) lit les vCard 2.1 à
4.0 (lignes repliées, échappements, quoted-printable des vieux Android, année absente d'iOS
écrite 1604) : nom, surnoms, numéros, adresses, anniversaire, organisation ; le reste (photos,
notes) est ignoré. Chaque fiche est journalisée une fois (`contact.card`), puis seulement si
elle change ; réimporter le même fichier ne fait rien. `contacts` (dans le tiroir) retrouve
quelqu'un par son nom, un surnom ou un numéro. Un anniversaire sur une fiche devient un
anniversaire suivi par le module des proches (`relation.birthday`), sauf sur sa propre fiche.

Le carnet reste un carnet : rien n'en devient un fait de la mémoire, et rien ne part. Pas de
synchronisation CardDAV pour l'instant : l'export suffit, et réimporter met à jour.

## D157 — Un seul « plus tard » : `later` (chantier 3)

*27/10/2026 · acceptée (complète D151)*

D151 avait laissé de côté la fusion des façons de remettre à plus tard, parce qu'elle
obligeait à réécrire leurs événements. Elle ne les réécrit pas : `later` n'est jamais
exécuté, le registre le transforme en l'appel qu'il remplace avant qu'il tourne
(`Tool.route`), comme le tiroir le fait pour `orion`. Le gardien, le journal, `planned`, les
évaluations et la relecture finale voient `remind_me`, `follow_up`, `awaiting`, `think_later`
ou `mission`, comme avant.

Le modèle dit quand (`at`, `every_hours`, ou `reply_from` pour une réponse attendue) et quoi
faire alors (`then`) : le lui dire (`remind_me`), lui demander comment ça s'est passé
(`follow_up`), y réfléchir cette nuit (`think_later`), y travailler à un rythme (`mission`),
ou annuler (`cancel`, avec l'identifiant que montre `planned`, qui dit désormais
« later (cancel, …) »). Un appel incomplet ne se transforme pas : il revient en erreur, avec
ce qui manque. Les outils remplacés quittent le menu sans entrer dans la liste du tiroir
(`ToolRegistry(..., behind=…)`) : ils restent appelables par leur nom.

27 outils au lieu de 31, 30,3 k caractères de schémas au lieu de 32,7 k.

## D158 — Une constitution courte (chantier 3)

*27/10/2026 · acceptée*

Le message système faisait 33,2 k caractères avec les modules par défaut. La constitution
(13,9 k) mêlait ce qu'Orion est et le mode d'emploi de ses outils, que leurs descriptions
disent déjà là où le modèle les lit. Elle est réécrite en une page (6,6 k) : sa façon de
parler (avec ses deux exemples), de travailler, de le connaître, sa mémoire, la confiance,
ce qu'il reçoit et l'initiative. Ce qui ne servait qu'à un outil rare (provenance,
confinement d'une source, `people` same, `forget_fact`) est dans l'outil. Les offres et la
section des photos sont resserrées. Le message système par défaut tombe à 24,4 k (35,8 k
avec tous les modules, au lieu de 43,3 k), sous l'objectif de 25 k, que
`tests/test_prompt_budget.py` vérifie désormais.

Mesure, évaluations hors web sur Haiku 5.5, un passage : 44/48 avant (D156), 40/48 après
D157 et D158. Les cinq cas en écart, rejoués trois fois de chaque côté, donnent les mêmes
scores (`un_cadeau_pour_maman` 3/3 et 3/3, `ce_quil_sait_de_lea` 3/3 et 3/3,
`voit_venir_le_dentiste` 2/3 et 2/3, `relance_le` 1/3 avant et 2/3 après,
`demain_a_1h_du_matin` 0/3 des deux côtés, déjà en échec avant la refonte) : du bruit, pas
une régression.

## D160 — Ses lectures : les flux qu'il suit (chantier 6, troisième source)

*27/10/2026 · acceptée*

Le module `feeds` (actif par défaut, rien à faire tant qu'il ne suit aucun flux) lit toutes
les trois heures les flux RSS ou Atom de ce qu'il suit, déclarés dans orion.toml
(`[modules.feeds] urls`) ou par message (`feeds` add, dans le tiroir : « suis le blog de… »).
Seules les adresses publiques sont lues, comme pour le web, et un document qui déclare une
DTD ou des entités est refusé. Ce qui est nouveau est journalisé (`feed.item`, contenu web,
`Trust.WEB`) ; d'un flux qu'on vient d'ajouter, seuls les cinq derniers articles : ses
archives ne sont pas des nouvelles.

Branché sur la curiosité (D76) : chaque semaine, son juge pèse les articles de la semaine à
côté de ce que ses recherches ont trouvé, et peut en garder un pour lui. La piste, elle, est
toujours choisie à partir de sa mémoire seule : rien de ce que dit un flux ne peut orienter ce
qu'Orion cherche, et le juge ne voit jamais sa mémoire. `feeds` (list) dit ce qu'il y a de
neuf dans ses lectures.

## D161 — Ses colis, ses abonnements, sa maison (chantier 6, suite)

*27/10/2026 · acceptée*

**Colis et abonnements.** Pas d'extracteur de plus : Orion lit déjà chaque mail lui-même
(D114). La consigne qui accompagne un mail nouveau (`mail_notice`) lui dit désormais quoi en
faire : un colis en route devient une situation jusqu'au jour de livraison (D149) ; un essai
gratuit qui deviendra payant, un abonnement qui se renouvelle ou augmente, un engagement dû
la veille, pour qu'il puisse résilier à temps. Les deux apparaissent dans `<maintenant>` et
dans la veille, qui les voient venir. Évaluations sur Haiku 5.5, trois passages :
`un_colis_en_route` 0/3 avec l'ancienne consigne, 3/3 avec la nouvelle ;
`un_essai_gratuit_devient_payant` 3/3 dans les deux cas.

**Sa maison.** Le module MCP (D95) branche déjà Home Assistant, qui expose un serveur MCP. Il
manquait la marche à suivre : la skill `maison-home-assistant` du catalogue la donne
(l'intégration à ajouter, le jeton par le formulaire du gardien, le serveur `maison` avec ses
outils de lecture en `read_only`) et dit comment s'en servir : lire l'état sans demander, agir
avec son accord, ne pas réciter la maison sans raison.

## D162 — La barre dit ce qu'il fait, ses mots deviennent des messages

*27/10/2026 · acceptée · révise D143*

La bulle de travail disait surtout « Je lis la page », « Je cherche sur le web », « Je regarde
ce que j'ai » : l'outil, jamais ce qu'il en fait. Et ce qu'Orion écrivait en chemin, rare
(la constitution le retenait), finissait dans cette bulle au lieu de lui parler.

- **Chaque appel dit ce qu'il fait, d'après ses arguments** (`Tool.doing_of`) : « je lis
  lemonde.fr », « je cherche « vols Lisbonne » », « je regarde les mails de Paul ». Sans modèle :
  instantané et gratuit. Après un contenu venu d'ailleurs, seul ce que la forme d'un argument
  garantit peut paraître (le domaine d'une adresse), jamais son texte libre ; sinon le `doing`
  de l'outil. Des appels lancés ensemble se lisent d'une ligne (« Je lis lemonde.fr,
  lefigaro.fr ») ; entre deux appels, « Je fais le point… ». `recall`, `person` et `agenda` ont
  maintenant leurs mots.
- **Ce que le modèle écrit avec un appel d'outil est un message**, dans l'application aussi
  (`Progress` dit si le texte vient de lui). Ce que dit le travail (la phrase de progression,
  le plan) reste dans la bulle, comme en D143.
- **La constitution ne le retient plus, elle l'oriente** : ne jamais annoncer ce qu'il fait
  (la bulle le montre), mais écrire tout de suite ce qu'il apprend ou décide en chemin et qui
  change quelque chose pour le propriétaire. Pas d'outil `say` : un modèle oublie de l'appeler,
  alors que le texte à côté d'un appel, il l'écrit déjà. S'il se tait, la bulle suffit.
- Évaluations : `il_dit_ce_quil_trouve_pas_ce_quil_fait` (aucune annonce de travail en chemin)
  et son témoin `une_question_simple_sans_parole_en_chemin` (un tour court ne parle qu'une
  fois). Le cache de prompt est intact : rien de volatil n'entre dans le message système.

Pas fait ici : une réponse en plusieurs messages (la réponse, puis une question à part). À
reprendre si les messages en chemin tiennent leurs promesses.

## D163 — Il parle en plusieurs messages quand il a plusieurs choses à dire

*27/10/2026 · acceptée · complète D162*

Un ami qui répond par texto n'envoie pas un pavé : la réponse, puis, à part, la question ou la
remarque qui vient après. Couper une réponse en paragraphes ne ferait que l'imiter ; c'est
Orion qui décide où un message s'arrête, parce qu'il a deux choses à dire.

- **`<suite/>` dans sa réponse commence un autre message**, trois au plus (le reste rejoint le
  dernier). La constitution le dit en une ligne : deux choses à part, jamais pour couper.
- **Chaque message suivant arrive comme tapé** : le canal montre qu'il écrit (les trois points
  dans l'app, l'indicateur sur iMessage et Telegram), une pause qui croît avec la longueur
  (`beat`, trois battements au plus), puis le message. Ce qu'il écrit de lui-même (initiative)
  se coupe pareil, sans indicateur.
- **Le journal garde la réponse telle qu'il l'a écrite**, balise comprise : la conversation
  rejouée au modèle ne change pas, le cache de prompt est intact. Dans l'app, la trace du
  travail se replie au-dessus du premier message. Une balise restée ailleurs (une légende)
  devient un saut de ligne, jamais du texte.
- Évaluation `deux_messages_seulement_pour_deux_choses` : au plus deux messages pour une
  nouvelle, et aucun morceau de phrase. Pour tenir le budget de la constitution, la phrase
  sur les identifiants des souvenirs est partie (chaque bloc les montre, `remember` dit
  comment s'en servir).

iMessage coupait déjà une réponse à chaque paragraphe (`split_bubbles`) : inchangé ici.

## D164 — Orion est root sur son serveur

*27/10/2026 · acceptée · revient en partie sur D28 et D96*

Sans root, Orion ne pouvait pas lire ses propres logs (`journalctl`), ni redémarrer un service,
ni installer un paquet : il était trop limité pour s'occuper de sa machine. Le propriétaire veut
qu'il ait les droits root, et il connaît les risques.

- **`orion-core` est root par `sudo -n`** (`deploy/orion-core.sudoers` : `NOPASSWD: ALL`). Le
  fichier est vérifié par `visudo` avant d'être installé dans `/etc/sudoers.d/orion-core`, par
  `install.sh` et à chaque mise à jour (`orion-update`). Une version sans ce fichier retire la
  règle, et `uninstall.sh` la supprime.
- **L'unité du core perd `NoNewPrivileges` et `ProtectSystem=strict`.** Sans ça, sudo échoue ou
  le système reste en lecture seule, même pour root.
- **Ce qui protège encore :** le shell reste désactivé par défaut et son activation reste le
  réglage du propriétaire (D92). Chaque commande lui est montrée, exacte, avant de s'exécuter
  (D96).
- **Ce qu'on perd :** D28 ne tient plus contre un core compromis. Root lit les secrets du
  guardian (`/etc/orion/guardian.env`, `/srv/orion/guardian`). Le guardian reste le seul
  chemin normal vers le monde (outbox, politiques), mais ce n'est plus une frontière de
  sécurité face au core.

## D165 — Un redémarrage réveille Orion

*27/10/2026 · acceptée*

Quand Orion redémarrait après un changement (un réglage, `systemctl restart` par sudo, une mise
à jour), personne ne l'appelait ensuite : ce qu'il avait prévu de faire après le redémarrage
(vérifier qu'un réglage a pris, reprendre une tâche, dire où il en est) restait en plan.

- **Au premier tour du moteur d'initiative après le démarrage**, Orion est réveillé s'il a parlé
  dans le fil du propriétaire moins de 15 minutes avant l'arrêt (`WOKEN_WITHIN`). Un Orion
  inactif depuis longtemps n'est pas réveillé : il n'avait rien en cours.
- **C'est une initiative comme une autre** (clé `restart:<seq>`), journalisée, qui passe même
  aux heures calmes (comme un rappel demandé), mais il peut répondre `SILENCE` : rien ne part
  alors.
- **Pas de boucle :** après 3 réveils de suite sans message du propriétaire entre eux
  (`WOKEN_AT_MOST`), un redémarrage ne le réveille plus.

## D166 — Des rappels qui reviennent, et qu'on repousse

*27/10/2026 · acceptée · complète D157*

« Tous les lundis à 8h », « chaque 1er du mois » : Orion ne savait poser qu'un rappel à la
fois, et une mission (`every_hours`) n'est pas faite pour ça. Repousser un rappel (« redis-le
moi dans une heure ») demandait de l'annuler puis d'en poser un autre.

- **`repeat` sur un rappel** (`later` tell, `remind_me`) : chaque jour, jour de semaine,
  semaine, mois ou année, à la même heure locale. Une série n'a qu'une occurrence en attente :
  quand elle part, la suivante est programmée dans le même ajout au journal, sous un nouvel
  id. Annuler l'occurrence en attente arrête la série.
- **Le rythme se compte depuis le premier rappel** (`anchor`, dans le fuseau du propriétaire) :
  le 31 de chaque mois tombe le 30 ou le 28 sans glisser ensuite, et 9h reste 9h au
  changement d'heure. Les occurrences manquées pendant un arrêt sont sautées, pas envoyées
  d'un coup.
- **Déplacer un rappel** : `reminders` move, ou `later` tell avec son `id` r_… et un nouveau
  `at`. Le texte, l'appel et le rythme sont gardés.
- Un rappel sans rythme garde exactement le payload d'avant : les journaux existants se
  relisent tels quels.

## D167 — Il peut tout faire seul quand le propriétaire le lui dit, le shell compris

*27/10/2026 · acceptée · revient en partie sur D96 et sur la liste fermée de l'autonomie*

Le propriétaire voulait pouvoir dire « ne me demande plus » pour n'importe quel outil, le
shell compris : avec root (D164), redemander chaque commande rendait Orion lent à s'occuper de
sa machine. Mais l'autonomie (`autonomy_grant`) ne s'accordait que pour trois actions
listées dans `[guardian] autonomable`. Il connaît le risque : un Orion qui lance seul des
commandes root peut tout changer sur le serveur.

- **`*` dans `[guardian] autonomable`** (le défaut) : toute action peut être accordée quand
  il la demande, par outil (`shell`, `mail_tidy`, `delegate`…). Il dit oui une fois à
  l'accord lui-même, vérifié par le guardian comme tout accord (D28). Les actions nommées
  restent offertes par le rituel mensuel. Une action sous `*` ne l'est jamais : il la demande.
- **Jamais seul :** ajouter ou retirer du code à Orion (`NEVER_ALONE`, D105). Les limites
  exigées restent exigées : des destinataires pour un mail ou un proche, des domaines pour le
  navigateur.
- **Jamais depuis du contenu extérieur :** une autonomie ne joue que dans une conversation
  sans contenu venu d'ailleurs. Avec un mail ou une page dans la conversation, il redemande.
  Sans ça, un mail piégé lancerait des commandes root.
- Il reprend la main d'un mot (`autonomy_revoke`), dans l'app ou la console. Un réglage sans
  `*` revient à la liste fermée d'avant.

## D168 — Une autonomie tient même avec du contenu extérieur

*27/10/2026 · acceptée · revient sur une règle de D167*

Le propriétaire ne veut plus qu'Orion lui redemande une action qu'il lui a laissée, même
quand un mail ou une page est dans la conversation. Il sait ce que ça veut dire : un mail ou
une page piégés peuvent faire lancer à Orion, sans lui demander, toute action qu'il lui a
laissée. Avec le shell, ça veut dire des commandes root, la lecture des secrets comprise.

- **Une autonomie accordée joue quel que soit le contenu de la conversation.**
- **Ce qui reste :** ajouter ou retirer du code à Orion n'est jamais fait seul, ni depuis du
  contenu extérieur (`NEVER_ALONE`, `NEVER_FROM_UNTRUSTED`). Accorder une autonomie
  (`autonomy_grant`) reste une action qu'il approuve lui-même.
- Revenir en arrière : retirer l'autonomie (`autonomy_revoke`, l'app, la console).

## D169 — Plus de barre : il parle par messages, et dit ce qu'il a trouvé

*27/10/2026 · acceptée · revient sur D143 et sur la barre de D162*

Le propriétaire veut avoir l'impression d'écrire à quelqu'un, pas de regarder une machine
travailler. Une personne ne montre pas une barre de progression : elle écrit quand elle a
quelque chose à dire, et on voit qu'elle tape.

- **Plus rien ne montre ce qu'il fait.** Plus de bulle de travail dans l'app, plus de trace
  repliée au-dessus de la réponse, plus de ligne écrite par un petit modèle quand il se tait,
  plus de plan montré (`plan` n'a plus `show`). Les outils n'ont plus de `doing`.
- **Pendant le travail, on voit seulement qu'il tape** : les trois points dans l'app,
  l'indicateur de frappe sur Telegram et iMessage. Le bouton pour arrêter reste dans l'app.
- **Ses mots écrits avec un appel d'outil restent des messages (D162).** La règle change : il
  ne dit jamais ce qu'il va faire. Il dit ce qu'il vient de trouver ou de faire, quand ça
  compte pour le propriétaire. Sinon, il ne dit rien.
- **Seul le dernier message d'un tour sonne.** Ceux écrits en chemin partent sans
  notification (`quiet`, journalisé dans l'outbox et repris tel quel après un crash). C'est
  `disable_notification` sur Telegram, et pas de push dans l'app. iMessage n'a pas d'envoi
  silencieux : ses messages sonnent.

## D170 — L'app bouge comme une app : saisie, clavier, onglets

*27/10/2026 · acceptée · complète D139*

Le propriétaire trouvait la saisie d'Orion moche, le clavier qui casse la mise en page, les
changements d'onglet brutaux, et l'app loin des belles animations demandées.

- **Les trois points cèdent leur place au message dans la même image**, au lieu de s'effacer
  sous lui puis de faire sauter le fil. S'il continue à travailler, ils reviennent dessous un
  instant après. L'avatar a un halo et « écrit… » scintille pendant qu'il tape.
- **Le clavier pilote la mise en page, pas le focus.** Le composeur ne rend la place du dock
  qu'une fois le clavier là (`body.keyboard`, déduit du viewport visible) ; le dernier message
  reste en vue ; panneaux, feuilles et voix suivent eux aussi le viewport visible.
- **Les onglets se croisent** : l'ancien glisse et s'efface sous le nouveau, la lentille de la
  barre s'étire en chemin.
- **Des micro-interactions partout** : arrivée des bulles et des pièces, réponses rapides en
  cascade, accueil, champ du composeur, surbrillance des lignes, grand titre qui s'efface au
  défilement. Toujours transform et opacité ; rien quand le système réduit les animations.

## D171 — Le mail se lit en direct, aussi loin qu'il faut ; un navigateur mort se relance

*28/10/2026 · acceptée · complète D114*

Orion n'a pas pu fouiller 4-5 mois de mails : `mail` plafonnait à 30 jours, et ne lisait de
toute façon que la copie locale, qui ne remonte qu'à `lookback_days` (2 jours) avant le
démarrage. Le navigateur, lui, planta deux fois au lancement : le gestionnaire de crash de
Chromium réclamait `--database`, absent, et le processus finit en SIGTRAP.

- **`mail` lit la boîte telle qu'elle est à l'instant** : chaque liste synchronise d'abord,
  plus de copie vieille de cinq minutes. La lecture d'un mail (`read`) garde le cache.
- **Plus loin que la synchro, il cherche sur le serveur** : `days` va jusqu'à des années,
  `until_days` borne une fenêtre, `sender` vise. Un `SEARCH SINCE … BEFORE … FROM …`, toujours
  en lecture seule (EXAMINE, BODY.PEEK), les 50 plus récents par compte. Ce qui n'était pas
  connu devient des cartes comme les autres, journalisées, tiers ; rien n'est gardé deux fois.
  En mode séparé, c'est le gardien qui cherche (`/v1/mail/{compte}/search`).
- **Chromium reçoit un dossier de crash** (`--crash-dumps-dir`, rapporteur coupé) : sans
  dossier personnel, son gestionnaire de crash n'en trouvait pas et l'abattait au démarrage.
- **Un navigateur qui ne démarre pas le dit** au lieu de faire tomber l'outil, et **un
  navigateur mort se relance** : la session se sait perdue quand son contexte se ferme sans
  qu'on l'ait demandé ; l'appel suivant en ouvre une neuve, ses onglets étant partis avec.

## D172 — Chromium a un dossier de config à lui

*28/10/2026 · acceptée · corrige D171*

Le navigateur se fermait toujours à l'ouverture (`computer.launch_failed`, `TargetClosedError`).
Le correctif de D171 (`--crash-dumps-dir`) ne changeait rien : le gestionnaire de crash de
Chromium range sa base dans `~/.config/chromium`, et le home d'`orion-core` (`/srv/orion`)
appartient à root. Reproduit avec `HOME=/` : `chrome_crashpad_handler: --database is required`.

- **Chromium reçoit `XDG_CONFIG_HOME`**, un dossier temporaire inscriptible
  (`/tmp/orion-chromium`, privé au service par `PrivateTmp`). Le drapeau de D171 est retiré.
- **Un échec de lancement journalise les dernières lignes d'erreur de Chromium**, pas seulement
  la première ligne de Playwright, qui ne dit jamais pourquoi.

## D173 — Un outil `tell` pour parler en chemin

*28/10/2026 · acceptée · revient sur le « pas d'outil `say` » de D162*

D162 refusait un outil `say` : « le texte à côté d'un appel, il l'écrit déjà ». Pas tous les
modèles : GPT 6 Luna n'écrit rien à côté de ses appels, et le propriétaire ne recevait qu'un
message final là où il demandait un mot à chaque étape. GPT 6 Sol, lui, le faisait.

- **`tell` (texte) envoie un message tout de suite et le tour continue.** Le runtime l'envoie dès
  que le modèle fait l'appel, comme le texte écrit à côté d'un appel (même outbox, sans sonner,
  D169) ; l'outil lui-même ne fait rien. Il s'appelle avec l'outil suivant, pas seul.
- **Le texte à côté d'un appel reste un message** : deux voies, celle que le modèle prend.
- **Mêmes règles que D169** : ce qu'il vient de trouver ou de faire, jamais ce qu'il va faire.
- Évaluation : `plusieurs_etapes_plusieurs_messages_un_seul_tour`, à passer sur Luna.
- **Il s'appelle `tell`, pas `say`** : `say` est déjà le vocal du module `vocal`, et deux outils
  du même nom empêchent Orion de démarrer (c'est arrivé au premier déploiement).
- **Chaque message ne dit que du neuf** (corrigé le jour même) : sur Hyperplanning, trois
  messages redisaient chacun la reconnexion et les notes d'anglais, et annonçaient la suite
  (« je vais maintenant ouvrir Bulletin »). La constitution et `tell` le disent ; la réponse
  finale ne redit pas ce qu'il a déjà reçu. Évaluation : `says_nothing_twice` et
  `no_narration` sur `plusieurs_etapes_plusieurs_messages_un_seul_tour`.

## D174 — Chaque soir, Orion relit et remet d'aplomb tout ce qu'il sait

*28/10/2026 · acceptée · remplace la consolidation par paires de D27 et D78*

La consolidation de nuit comparait les faits deux à deux par embedding : 40 paires au plus, les
plus proches d'abord, sans garder trace des paires déjà jugées. Avec une centaine de faits, elle
rejugeait chaque nuit les mêmes paires et ne lisait jamais le reste ; un fait sans voisin proche
n'était jamais relu. Et le code décidait de ce qui méritait un regard, pas Orion.

- **La nuit d'Orion (D117) devient sa passe du soir**, à 23:30 par défaut (`[modules.thinking]
  at`), et tourne chaque soir, même après une journée sans un mot : le temps seul date un
  souvenir.
- **Il a tout sous les yeux** : chaque fait de confiance avec son id, les situations, les
  engagements, ce qui est à venir, la conversation du jour. Il trie lui-même avec ses outils :
  `forget_fact` ce qui est dépassé, `remember` (`replaces`) pour réécrire ou fusionner, une
  `situation` pour ce qui ne dure qu'un temps ; il ferme les engagements tenus, corrige les
  rappels, missions et suivis que la journée a rendus faux, et note dans son `playbook` ce
  qu'elle lui a appris sur la façon d'être avec lui. Puis il avance ce qui est resté en plan.
- **Ce qui vient d'un mail ou d'une page n'est pas posé devant lui** (confiance sous le seuil) :
  ça ne pilote pas le tri. Il ne change que ce dont il est sûr ; ce que le propriétaire a dit
  ne s'efface que si la journée le dément ; un doute devient une question au réveil.
- **La consolidation de nuit (03:30) ne garde que le mécanique** : trier ce qui n'était pas
  durable en situations (D149), fermer les situations échues, réécrire le portrait (D144),
  désormais après le tri du soir. `similarity` et `max_pairs` disparaissent de
  `[modules.consolidation]` : une config qui les porte encore ne démarre plus (fail closed),
  il faut les retirer.

## D175 — Plus d'outil `current_time` : l'heure est dans chaque tour

*28/10/2026 · acceptée*

Chaque tour (propriétaire, initiative, tâche, contact) ouvre déjà son nouveau message sur
`[mardi 2026-10-28 14:32 · canal]`, l'heure du début du tour. Mais la constitution disait
« `current_time`, jamais deviné » : le modèle lisait cette ligne comme la date du message, pas
comme maintenant, et payait un appel d'outil pour l'heure.

- **L'outil est supprimé** ; la constitution dit que la ligne du dernier message, c'est
  maintenant, et que celles d'avant datent les messages.
- **Le cache ne bouge pas** : la ligne est en queue de requête, journalisée telle quelle, et le
  message système reste fixe (`tests/test_runtime_cache.py`). Un outil de moins change le
  registre une fois, au déploiement.
- Limite : pendant un long tour, l'heure n'avance pas. Les tâches longues repartent à chaque
  tour, donc avec une ligne neuve.

## D176 — La passe du soir voit toutes les rubriques, et chaque chose n'a qu'une place

*28/10/2026 · acceptée · complète D174*

Premier retour sur D174 : la réécriture des souvenirs est juste, mais beaucoup restent non
triés, et les rubriques se répètent (souvenirs, manuel, proches, à venir, missions). La passe
ne voyait que les souvenirs, situations et engagements ; le manuel, les rappels, les missions
et les proches, elle devait penser à les ouvrir, et ne le faisait pas : un doublon entre deux
rubriques lui était invisible. Au-delà de 400 souvenirs, elle ne relisait que les plus anciens.

- **Tout est posé côte à côte, avec les ids** : `<manuel>` (règles du playbook), `<prevu>`
  (tout ce qui est prévu sur 30 jours, D78, avec comment l'annuler), et ce que chaque module
  tient via un crochet `Module.review(now)` (missions : but, critères, rythme ; relations :
  anniversaires, rythmes, dernier contact). Un nouveau module s'y branche sans toucher la
  passe (D24).
- **Chaque chose n'a qu'une place**, dit la consigne : un fait qui dure est un souvenir, ce qui
  passe une situation, une dette un engagement, une date un rappel, une façon d'être le manuel,
  un but long une mission, un anniversaire ou un rythme `relations`. Un souvenir qui redit une
  autre rubrique est oublié, avec la raison ; mal rangé, il est rangé puis oublié.
- **Ce qui a échappé sur le moment** : la passe relit la journée pour ce qu'il a dit en passant
  (lui, ses proches, ses goûts, une date, une promesse) et ce qu'il a repris à Orion (le
  manuel).
- **Au-delà de 400 souvenirs, chaque soir prend la part suivante**, du plus récent au plus
  ancien : tout est relu en quelques soirs, sans curseur à journaliser.
- Limite : les notes d'une mission qui a lu le web ne sont pas posées (confiance) ; son but
  l'est. Les anniversaires et rythmes ne portent pas de confiance propre.

## D177 — Ses ratés, c'est Orion qui les relève le soir (remplace la détection de D152)

*28/10/2026 · acceptée · complète D152 et D174*

« Mes ratés » se remplissait par regex : un 👎, ou un message qui commençait par « non » ou
disait « c'est pas ça » juste après une réponse. Un « non » ordinaire passait pour une
correction, une correction tournée autrement passait à côté, et rien ne disait ce qui n'allait
pas.

- **La passe du soir juge** : elle relit la journée, et ses 👎 depuis la veille lui sont
  posés avec le message visé (`<pouces-baisses>`). Pour chaque échange raté (repris, répété,
  agacé, 👎), elle appelle `missed` : quand, ce qu'il avait demandé, ce qu'Orion a répondu,
  comment il l'a montré, et ce qui n'allait pas. Un simple changement d'avis n'en est pas un.
- **Événement `adaptation.missed`**, à la confiance du tour qui l'écrit. `orion misses`,
  `orion week` et l'app lisent ces événements ; plus aucune regex. La leçon qu'il en tire va
  toujours dans le manuel (`playbook`), pas dans les ratés.
- Limites : un raté n'apparaît que le lendemain de l'échange ; les ratés détectés avant ce
  changement ne sont pas repris. L'outil sert aussi le jour, si Orion se voit repris.

## D178 — Une fenêtre de conversation deux fois plus courte

*28/10/2026 · acceptée · complète D123*

Pendant qu'on parle, la fenêtre montait jusqu'à 120 000 tokens estimés avant un rebase qui la
ramenait à 60 000 : elle oscillait entre les deux, et chaque étape d'une boucle la relisait.
La partie fixe (constitution 25 k caractères, schémas 32 k) pèse environ 15 000 tokens ; la
fenêtre en pesait souvent quatre à huit fois plus.

- **`max_window_tokens` passe à 60 000** : la fenêtre oscille entre 30 000 et 60 000, soit
  plusieurs heures d'échanges. Ce qui sort est résumé (D43), regardé une dernière fois par la
  mémoire (D44) et reste trouvable par `conversations_search`.
- **Pourquoi la qualité ne baisse pas** : après une pause, Orion repart déjà de 16 000 tokens
  et d'un résumé (D123) sans perte constatée ; 30 000 au plus bas, c'est presque deux fois
  plus. Une fenêtre plus courte noie aussi moins la consigne du moment.
- Le rebase revient deux fois plus souvent, mais chacun recharge une fenêtre deux fois plus
  petite, et chaque étape entre deux en porte moitié moins.
- Non retenu : raccourcir encore les schémas ou la constitution sans évaluation pour le
  vérifier (D123, D158).

## D179 — Orion range aussi sa boîte : corbeille, déplacer, marquer

*28/10/2026 · acceptée · complète D75*

Orion lisait, envoyait, rédigeait des brouillons, archivait et désabonnait. Il ne pouvait ni
supprimer un mail, ni le ranger dans un dossier, ni le marquer lu ou suivi.

- **`mail_tidy` prend `trash` et `move`** (avec `folder`). Supprimer, c'est déplacer vers la
  corbeille du compte : `trash_folder`, sinon le dossier que le serveur marque `\Trash`
  (RFC 6154), sinon refus. Rien n'est jamais effacé pour de bon par Orion : le serveur vide sa
  corbeille comme il le ferait pour lui.
- **Toujours approuvés**, par lot comme l'archivage. L'autonomie de `mail_tidy` ne couvre que
  l'archivage et le désabonnement ; le gardien vérifie aussi que le dossier est celui approuvé.
- **`mail_mark`** : lu, non lu, suivi, plus suivi. Ce ne sont que des drapeaux IMAP, défaits
  par la marque inverse : pas d'approbation, comme un brouillon. Événement `mail.marked`.
- Limites : un brouillon déjà posé ne se modifie pas (on en pose un autre) ; on ne crée pas de
  dossier.

## D180 — `orion` se lance de n'importe où

*28/10/2026 · acceptée*

Les commandes demandaient `uv run orion …` depuis le dépôt : la config était cherchée dans le
dossier courant, et ailleurs Orion partait sans un mot sur ses réglages par défaut.

- **`uv tool install --editable .`** met `orion` dans le PATH. Éditable, il suit le dépôt après
  un `git pull` et garde l'accès à `catalog/` (skills, modules), qui n'est pas dans le paquet.
- **Où est la config sans `--config`** : `$ORION_CONFIG`, sinon `orion.toml` du dossier courant
  (un clone, le serveur), sinon `$ORION_HOME/orion.toml`, `~/.orion` par défaut. `data/` et les
  secrets suivent le fichier, comme avant ; `orion setup` écrit là aussi.
- Le serveur ne change pas : ses services passent `--config`.
- Non retenu : un paquet publié (PyPI) ; `catalog/` et le code servi au téléphone vivent dans le
  dépôt.

## D181 — Ses consignes reviennent quand la conversation s'en éloigne

*28/10/2026 · acceptée · complète D178*

Ses consignes (règles du manuel, préférences) n'étaient montrées qu'à l'ouverture de la fenêtre
ou après une coupe. Dans une longue conversation, elles finissaient loin derrière, noyées sous
les propres réponses d'Orion, qui reprenait peu à peu ses habitudes : « réponds en 1 à 5 mots »
tenait au début, plus au bout d'une heure.

- **Bloc `<rappel>`** : règles actives et préférences, rattaché au message du propriétaire dès
  que plus de 6 000 tokens (`REMIND_AFTER_TOKENS`) le séparent du dernier `<memoire>` ou
  `<rappel>`. Pas à chaque message : quelques rappels par fenêtre de 60k.
- Rattaché au message, jamais au prompt système : le préfixe en cache reste intact.

## D182 — Un plan inachevé ne laisse pas partir la réponse

*28/10/2026 · acceptée · complète D97, D118*

Sur une tâche longue, le modèle (GPT 6.1 vu en vrai) s'arrêtait à mi-chemin et rendait la main
avec un compte rendu, son propre plan encore à moitié fait. Le contrôle de fin (D118) ne lit que
la fin de la réponse (offres, promesses) : il ne voyait rien.

- **Le plan est lu par la boucle** : à chaque appel de `plan`, elle garde les étapes pas encore
  faites. Une réponse sans appel d'outil alors qu'il en reste ne part pas : Orion est renvoyé au
  travail avec la liste, et fait les étapes ou les retire du plan en disant pourquoi.
- **Trois fois au plus par tour, et seulement s'il a travaillé depuis** : un modèle qui répond
  encore sans rien faire a ses raisons (étape impossible, question pour lui), sa réponse part.
  Les plafonds de pas et de coût tiennent toujours.
- La description de `plan` le dit, pour qu'il ne soit pas surpris.
- Non retenu : un second modèle qui juge « fini ou pas » à chaque réponse (coût et latence à
  chaque tour, pour un cas que le plan rend déjà visible).

## D183 — Maintenant se lit d'un coup d'œil, et Orion a un logo de planète

*28/10/2026 · acceptée · complète D138*

L'écran Maintenant empilait cinq ou six sections de même poids. Il commençait par une phrase
générique, et la barre de l'agenda mangeait la moitié de chaque ligne.

- **La carte d'état porte la journée** : avatar plus grand, sur un léger fond bleu. Dessous, des
  pastilles (« 2 pour toi · 2 aujourd'hui · 3 en cours ») qui mènent chacune à sa section.
  Elles n'apparaissent qu'à partir de deux, sinon elles répètent la page.
- **Ordre** : ce qui attend le propriétaire, puis son agenda, puis ce qu'Orion porte. Tâches,
  attentes et missions forment une seule liste, « De mon côté ».
- **Logo** : une planète nacrée sur fond bleu, avec une orbite fine, un satellite et une étoile.
  Il remplace l'anneau et les trois étincelles. Le dessin a été fait par Codex, en trois séries
  de variantes, et choisi par le propriétaire.
- Le texte entre backquotes s'affiche en code dans les messages, au lieu de montrer les `.

## D184 — Ses consignes, sous leur nom et plus tôt

*28/10/2026 · acceptée · complète D181*

Avec le `<rappel>` de D181 déployé, Orion lâchait encore son manuel (« réponds en 1 à 5 mots »)
au fil de la conversation. Trois défauts :

- **Le nom** : partout ailleurs, un « rappel » est un minuteur, et la constitution ne disait rien
  du bloc. Il devient `<consignes>`, et la constitution dit qu'il remet le manuel sous les yeux
  d'Orion, à appliquer à sa réponse.
- **Le seuil** : 6 000 tokens, c'est des dizaines d'échanges quand les réponses sont courtes, et
  la dérive commence bien avant. Le bloc revient aussi après 6 messages du propriétaire
  (`REMIND_AFTER_MESSAGES`), au premier des deux seuils atteint.
- **Les lecteurs hors conversation** (brief vocal, vérificateur) lisaient le bloc comme une parole
  du propriétaire : il est retiré comme `<memoire>`.

## D185 — Le manuel entier, et sa façon de parler d'abord

*28/10/2026 · acceptée · complète D21, D184*

Son manuel réel comptait 36 règles, environ 7 000 caractères. Deux choses le faisaient perdre :

- **Coupé à l'ouverture** : `<memoire>` était plafonné à 2 400 caractères, règles comprises, les
  plus anciennes d'abord. Ses règles de style, les plus récentes, tombaient après le « - … » et
  revenaient dans `<manuel>`, présentées comme des changements à signaler. Le manuel entre
  maintenant toujours entier ; le plafond ne borne que les faits et les engagements.
- **Contredit par la constitution** : « trois mots appellent une ou deux phrases », des
  exemples en phrases complètes, « de l'humour seulement quand il vient tout seul ». La
  façon de parler est désormais dite par son manuel ; la constitution ne donne qu'un défaut qui
  cède à la moindre de ses règles, et ses exemples montrent quoi faire, plus quel style.

Sur le serveur, `orion` lance `orion-admin` (sous le bon utilisateur, avec ses secrets).

## D186 — Le sommeil d'Orion : tout revoir, un temps après l'autre

*28/10/2026 · acceptée · complète D117, D174, D176*

La passe du soir avait tout sous les yeux (D176), mais en un seul tour : la journée, le
manuel, les souvenirs, ce qui est en cours, puis le travail à avancer, d'une traite. Le manuel
n'y était qu'une ligne parmi d'autres, sans la marque [appris] : 36 règles, dont six qui
redisaient le même style et trois apprises qui contredisaient les siennes, passaient nuit après
nuit.

- **Le sommeil** : tout le matériel est posé une fois, côte à côte comme en D176 (les doublons
  d'une rubrique à l'autre restent visibles), puis Orion le reprend en temps successifs, un
  tour chacun dans le fil `night` : la journée (ce qui a échappé, les ratés), le manuel, les
  souvenirs, sa vie en cours (situations, engagements, prévu, missions, proches), puis il rêve
  (l'inachevé, les promesses, la curiosité), et se réveille sur la note du matin. Chaque temps
  finit sur une ligne de ce qu'il a changé, que les suivants voient : rien n'est refait.
- **Le manuel, une idée par règle** : une [appris] qu'une de ses règles contredit ou couvre se
  retire ; ses propres doublons se fondent en une seule règle qui garde ses mots et chacune de
  ses précisions, et la note du matin le lui dit ; deux de ses règles qui se contredisent : la
  plus récente si c'est clair, sinon une question au réveil.
- **Budget** : un seul pour la nuit ; chaque temps garde de quoi se réveiller. Un temps qui
  échoue arrête la nuit, sans note. Si c'est le fournisseur qui la coupe (forfait épuisé, en
  vrai le 28 octobre), rien n'est compté comme lu : la nuit reprend au temps qui a échoué,
  toutes les heures jusqu'au matin, puis le soir suivant relit tout. Chaque nuit garde
  comment chaque temps a fini.
- La consolidation de 3 h 30 (tri, portrait) et le réflecteur de la journée ne changent pas.

## D187 — Chaque arrivée réveille Orion, et ses consignes de lieu durent

*28/10/2026 · acceptée · révise D57*

Le propriétaire avait relié « Maison maman » ; le téléphone envoyait bien l'arrivée, Orion
répondait `"reminders": 0` et rien ne se passait. Le rappel avait été demandé « chez maman »,
et un rappel de lieu ne servait qu'une fois. Il voyait les lieux autrement : le téléphone
prévient Orion à chaque passage, et c'est Orion qui décide quoi en faire.

- **Chaque arrivée ou départ réveille Orion** : une alerte de l'initiative. Sans consigne, il
  prend l'initiative de ce qui aiderait là (chercher, noter, poser un rappel), propose ce qui
  touche le dehors sans le faire, et se tait quand rien ne vaut la peine. Heures calmes et budget du jour s'appliquent ; passée une demi-heure, c'est trop
  tard. Le signal reste une information (`OWNER_INSTANCE`), pas un ordre.
- **Ses consignes de lieu** : une fois (comme avant), une fois jusqu'à un jour donné
  (« aujourd'hui » ne vaut pas la semaine suivante), ou à chaque fois jusqu'à ce qu'il les
  annule. Une consigne peut être une action, pas seulement un rappel : Orion la fait quand il
  arrive, puis lui dit ce qu'il a fait.
- **Ses mots, pas ceux du téléphone** : le message qui déclenche une consigne ne contient que
  ce que le propriétaire a dit (le lieu sous son nom à lui, quoi faire) ; le téléphone ne dit
  que quand. Il porte donc la confiance de la consigne, plafonnée à `SYSTEM`, et ce qu'elle
  fait en interne se fait sans lui redemander. Agir dehors (écrire à sa mère) passe par le
  gardien comme partout : Orion lui propose, en posant la consigne, de ne plus demander pour
  cette action, bornée à qui elle vise (D168, `autonomy_grant`).
- **Les noms** : « chez », « au », « des »… ne comptent plus (« chez maman » = « Maison
  maman »), et une consigne pour un lieu que le téléphone n'a jamais signalé rappelle à Orion
  les noms qu'il signale.

## D188 — Ce qu'Orion sait de lui : lisible, daté, corrigeable sur place

*29/10/2026 · acceptée · révise D11, D83, D125*

« Mes souvenirs » montrait une liste à plat. Ce qu'il avait dit en discutant s'affichait
« Vient de : thread:… », l'historique montrait « superseded by f_… », la recherche de l'app ne
trouvait que les mots exacts quand Orion, lui, cherche par le sens, et corriger renvoyait au chat.
La mémoire elle-même laissait passer deux choses : un contenu venu du dehors pouvait remplacer ce
qu'il avait dit, et un même fait redit s'ajoutait une deuxième fois.

- **D'où ça vient, en clair** : « tu me l'as dit », « tu me l'as dit en discutant », « j'en ai
  déduit », « lu dans un mail », « vu sur le web »… et quand, à partir de la source, de l'acteur et
  de la confiance de l'événement. La confiance d'un fait extrait reste plafonnée à `SYSTEM` :
  seule l'étiquette change.
- **Rangé par thème** : Toi, Tes goûts, Tes proches (un groupe par personne, alias suivis), Tes
  projets, Divers ; en tête, ce qu'il a épinglé et ce qu'Orion a appris cette semaine.
- **À vérifier** : ce que le module doute (D83) juge périmé est visible dans l'app, avec
  « Toujours vrai » (`doubt.confirmed`) et « Ça a changé » (modifier sur place).
- **Corriger sur place** : le texte se modifie dans l'app ; c'est un nouveau fait qui remplace
  l'ancien (`supersedes`), jamais une réécriture. Chaque fait sait qui l'a remplacé
  (`replaced_by`), donc ses versions se lisent en chaîne.
- **Épingler** (`memory.fact.pinned`) : un fait épinglé ouvre toujours `<memoire>`, quelle que soit
  sa catégorie, et le reste une fois corrigé.
- **Oublier s'annule** : quelques secondes pour « Annuler », puis « Le remettre » depuis
  l'historique. Remettre est un nouveau fait dit par lui, l'oubli reste au journal.
- **Recherche de l'app = celle d'Orion** : par le sens et par les mots (D27).
- **Gardes dans le code** : ce qui vient du dehors (sous `SYSTEM`) ne remplace ni ne retire un
  fait plus sûr que lui (`MemoryRefused`) ; un fait redit mot pour mot (casse, accents,
  ponctuation à part) n'est pas stocké deux fois, et s'il est redit par une source plus sûre, la
  nouvelle version prend sa place.
- **Le magasin se reconstruit seul** quand son schéma change (`SCHEMA_VERSION`) : il est
  rejoué depuis le journal, ce qu'il peut toujours être.

Pas fait, volontairement : pas de doublons « de sens » détectés par embeddings (« Paul aime le
café » et « Paul n'aime pas le café » sont trop proches pour qu'un seuil tranche ; la passe du
soir et l'extracteur s'en chargent), pas d'index vectoriel (le calcul exhaustif reste de l'ordre
de la milliseconde à l'échelle d'une vie), pas de table de personnes (elles restent dérivées des
faits, D27).


## D189 — Orion gère son agenda : déplacer, modifier, annuler

*29/10/2026 · acceptée*

L'adresse iCal secrète reste en lecture seule : c'est un flux, rien ne s'écrit par elle. Écrire
passe par le même accès que `calendar_add` (CalDAV ou Google, D32), qui sert maintenant aussi à
`calendar_change`.

- **Désigné par ce qu'il a vu** : le modèle nomme l'événement par son titre et son début, tels
  qu'`agenda` les montre. Le cœur retrouve son UID ; le gardien relit l'événement chez le
  fournisseur et refuse s'il n'a plus ce titre ou ce début (il a changé depuis l'accord).
- **Comme s'il cliquait lui-même** : Orion ne s'interdit rien que l'agenda permet. Un événement
  avec invités se change, et les invités sont prévenus par l'agenda (`sendUpdates=all`, ou
  l'ordonnancement CalDAV). Une invitation se retire de son agenda ; si l'organisateur est seul à
  pouvoir la changer, le fournisseur refuse (403) et Orion le dit.
- **Séries** : par défaut, cette date seule (exception `RECURRENCE-ID` ou `EXDATE` en CalDAV,
  instance chez Google) ; `series` change toute la série, décalée d'autant que cette date.
- **Accord exact** : la demande dit l'événement et chaque changement ; le gardien ne laisse passer
  que ce changement-là, une fois. Annuler est une suppression, dite comme telle. Sans fin donnée,
  un déplacement garde la durée.
- **Seul s'il le veut** : `calendar_change` fait partie des actions qu'Orion propose de faire
  seul après cinq accords d'affilée, ou dès qu'il le demande (D167). Le récap du soir le dit.
- **Visible tout de suite** : `calendar.event.changed` remplace l'occurrence dans la projection, et
  la copie plus ancienne du flux est ignorée un jour (`FEED_LAG`), le temps qu'il rattrape.
- CalDAV : `REPORT` par UID, puis `PUT` ou `DELETE` avec `If-Match` (rien n'écrase une
  modification faite entre-temps). Google : `iCalUID` (et `instances` pour une série), puis
  `PATCH` ou `DELETE`.

## D190 — Utilisation : tokens, messages, forfaits

Un écran « Utilisation » (Réglages) montre ce qu'Orion consomme, compté depuis le journal
(`orion.mobile.usage`, `GET /app/api/usage`) : par jour sur trente jours, aujourd'hui, la
semaine, depuis le début.

- **Chaque appel journalisé** : `spend.recorded` est écrit pour tout appel, un forfait compris (0 $),
  avec ses tokens (lus, écrits, relus du cache, mis en cache) et le fournisseur. Les appels plus
  anciens ne comptent que pour leur coût.
- **Messages** : les siens (`message.received`, par canal), les réponses d'Orion sur le fil du
  propriétaire, les outils lancés.
- **Forfaits** : ce que le fournisseur dit de ses fenêtres avec la réponse — Claude par le
  `rate_limit_event` de `claude --output-format stream-json`, ChatGPT par les en-têtes
  `x-codex-primary-*` / `x-codex-secondary-*`. Journalisé (`plan.quota`) quand ça bouge d'au moins
  un point : l'écran montre la dernière valeur et quand la fenêtre repart.
- **Crédit prépayé** : OpenRouter est interrogé à l'ouverture de l'écran (`/credits`, ou `/key`
  pour une clé qui n'y a pas droit) ; ce n'est pas journalisé, c'est l'état du compte.

## D191 — Moins de bruit : ses notes, les brouillons rejetés, un message par point

Le 30/10 au matin, dix messages entre 6 h et 8 h 40 : des notes qu'Orion s'était laissées
(« Tâche interne… Ne pas répéter ce rappel à Noan ») envoyées comme rappels, des points refusés
deux fois par la relecture partis quand même en texte brut, deux rappels et le point de 8 h 30 en
quatre minutes.

- **Ses notes à lui** : `later` gagne `check` (« vérifie à 6 h si j'ai cours », un travail à
  reprendre) ; le rappel porte `own`. À l'heure dite il réveille Orion, qui ne lui écrit que si ça
  le concerne : pas de texte de secours, un SILENCE n'envoie rien, la série continue.
- **Deux fois rejeté** : ce qu'il n'a pas demandé est abandonné (`initiative.suppressed`, raison
  `rejected: …`), plus envoyé brut. Ce qu'il a demandé garde son texte de secours (D103).
- **Un message par point** : un rappel ou un compte rendu qu'il a demandé, prêt moins de dix
  minutes avant un point, l'attend ; prêt en même temps qu'autre chose, il part dans le même
  message. Il passe toujours, heures calmes ou pas. Les appels, ses notes à lui et le réveil
  après redémarrage restent à part.

## D192 — Le sommeil se demande de quoi il a besoin

*30/10/2026 · acceptée · complète D186*

Le sommeil remettait d'aplomb ce qu'Orion savait, sans se demander si ce qu'il avait en place
servait encore, ni ce qui manquait.

- **Un temps « besoins »**, entre sa vie en cours et les rêves, les soirs où il a parlé : ce
  qu'il demande souvent à la main devient une veille, un flux, un rappel qui revient ou une
  règle du manuel ; un intérêt qui naît s'ajoute à la curiosité, ce qui ne sert plus (intérêt,
  veille, flux, rappel) se retire ; ce qu'Orion ne sait pas faire, il cherche un skill et le
  dit au réveil. Rien sans un signe concret dans ce qu'il a dit ou fait ; ce qui touche le
  dehors se propose au réveil.

## D193 — Le sommeil garde tout sous les yeux, répond à ses promesses, ne se rappelle rien

*30/10/2026 · acceptée · complète D186*

Relu la nuit du 29 octobre en vrai : un bon tri, mais trois défauts.

- **La fenêtre coupait le matériel** : après trois temps, le fil `night` dépassait sa fenêtre et
  le matériel posé au premier temps partait pour un résumé ; sa vie en cours et la suite
  relistaient tout à la main. Quand un temps ferait couper la fenêtre, le matériel est reposé
  avec lui, tel que les temps précédents l'ont laissé (`Orion.cuts`).
- **Une question promise restait sans réponse** : la note ne parlait que des recherches
  lancées. La note du matin donne, en plus de ses points, la réponse à chaque question promise.
- **Un rappel pour lui-même lui était envoyé** : « Tâche interne : récupérer les résultats… »
  est parti chez le propriétaire à 8 h 30. Un rappel part mot pour mot : la nuit n'en pose que
  pour lui, et les tâches de fond lui écrivent d'elles-mêmes.

## D194 — Le matin, Orion propose de raconter sa nuit

*30/10/2026 · acceptée · révise la note du matin de D117*

La note du matin arrivait d'un bloc, un pavé au réveil. Le propriétaire préfère qu'Orion lui
propose d'abord de raconter.

- **Une phrase courte** : Orion dit sur quoi sa nuit a porté et propose de la raconter. La
  note entière est dans le fil (l'initiative la porte) : s'il dit oui, Orion la déroule avec
  ses mots.
- Seul ce qui ne peut pas attendre sa réponse (un rendez-vous dans l'heure) se dit tout de
  suite, en une ligne.
