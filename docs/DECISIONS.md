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
