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
- trois points par jour (8h30, 13h, 19h), construits à partir du registre des engagements ;
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
- Le cache de prompt n'est pas appliqué : le serveur remplace `prompt_cache_key` à chaque
  requête. Sans coût par jeton, cela ne coûte que de la latence.
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
