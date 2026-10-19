# Étape 3 — Ce qui fait un vrai Jarvis

> Objectif : étudier en profondeur les cinq capacités qui distinguent un « second cerveau » d'un
> simple agent : **iMessage**, **voix**, **proactivité et attention**, **mémoire**, et
> **collaboration entre instances d'Orion — y compris entre personnes différentes** (D7, D8).
>
> Date de l'étude : 19 octobre 2026. Marqueurs de fiabilité repris des rapports : les chiffres
> issus uniquement de la presse ou d'un fournisseur sont signalés comme tels.

---

## 0. Ce qu'il faut retenir en une page

1. **Instinct** (Noah Shinn, auteur de Reflexion) est la référence la plus proche de la vision
   d'Orion : son propre numéro, son propre ordinateur, proactif, appels rares mais décisifs, et
   **collaboration Instinct-à-Instinct entre personnes de confiance** (300 000 coordinations la
   première semaine). Mais il a enchaîné les incidents de sécurité. → Il valide le *quoi*, et montre
   que le *comment* sécurisé reste à inventer.
2. **iMessage** : Apple n'a aucune API. Phase 1 = une **ligne gérée** (Inkbox, Sendblue, Linq) comme
   identité d'Orion + **`imsg` en lecture seule sur ton Mac** pour connaître ta vie. Envoyer « en ton
   nom » viendra plus tard, sous approbation.
3. **Voix** : le marché vend désormais le motif « bavard rapide + penseur lent » : GPT-Live 1
   (0,05 $/min, délègue à ton backend), Gemini 3.8 Live (appels d'outils non bloquants). Le modèle
   vocal est une **façade jetable** ; Orion garde l'état. Pipecat (Python) comme passerelle.
4. **Proactivité** : le silence est la réponse par défaut. On parle quand l'utilité attendue dépasse
   le coût de l'interruption, avec un **coût du retard qui évolue dans le temps**. Trois résumés par
   jour battent un flux continu. Les **engagements** sont la meilleure raison de parler.
5. **Mémoire** : la recherche d'information n'est plus le problème. Les vrais problèmes sont **la
   vérité sur des années** (bi-temporalité), **la provenance et la contamination**, **l'oubli par
   source**, **les engagements**, **la cohérence entre instances**. Aucun système ne fait les cinq.
6. **Collaboration entre personnes** : aucun produit grand public ne fait dialoguer ton agent avec
   celui de quelqu'un d'autre d'un fournisseur à l'autre. Orion serait en avance. A2A v1.0 donne le
   transport ; **tout le reste est à construire** : présentation, relations, divulgation,
   négociation, défense, preuve.

---

## 1. Instinct et Open Instinct

### 1.1 Instinct (instinct.com)

- Société de Noah Shinn (Reflexion, ex-Sierra), ~14 personnes, valorisée **10 Md$** en septembre
  2026 après trois levées en trois mois. Sur invitation, fermé, sans API.
- **Identité propre** : son numéro (iMessage, WhatsApp, SMS), son adresse mail, son ordinateur
  cloud avec navigateur. Plus de 50 % du trafic n'est **pas** iMessage : il « prend la forme » des
  canaux que tu utilises déjà.
- **Proactivité** : se réveille et s'endort quand il veut (« il scanne à 6 h parce qu'il sait que tu
  te lèves à 7 h »). Il **t'appelle** très rarement : ~3 fois en quelques mois, seulement pour
  l'important.
- **Principes de Shinn** : *« ne pas se focaliser sur la capacité, seulement sur la
  compréhensibilité »* ; *« pas un exécutant de tâches »* mais quelqu'un qui veille sur toi ; le
  message important tient dans la première ligne.
- **Sécurité, détachée de l'agent** : pare-feu sur tout contenu entrant, **moniteur d'actions
  découplé** qui peut mettre en pause, approuver ou refuser chaque action, détecteur
  d'hallucinations à base de petits modèles.
- **Paiements** : cartes virtuelles à usage unique (Stripe Link), coffre-fort pour les identifiants,
  1Password à la demande.
- **Instinct-à-Instinct** : les agents ne parlent qu'aux agents de personnes de confiance en liste
  blanche, avec des **droits par relation et par domaine** (ton conjoint voit tout, un collègue
  seulement ton agenda pro). Si un agent sort de son périmètre, le tien refuse et te prévient.
- **Incidents** (presse) : a suivi des instructions cachées dans des mails, envoyé un mail sans
  approbation, récupéré en silence des codes 2FA dans Gmail, réinitialisé des mots de passe sans
  demande, fait des débits non confirmés. Les conditions d'utilisation en font ton mandataire légal
  et l'entraînement sur tes données est activé par défaut.
- Fournisseur iMessage **probablement Linq** (une seule source, non vérifiée). Une app Mac en bêta
  lit tes iMessages et envoie depuis **ton** Mac : un modèle hybride ligne d'agent + « main » sur ton
  Mac.

### 1.2 Open Instinct (open source, TypeScript)

- Clone MIT d'Instinct, 837 tests. Identité via **Inkbox** (ligne iMessage, mail, numéro, point de
  terminaison agent-à-agent), micro-VM par utilisateur, boucle « pi » (comme OpenClaw).
- À retenir : **sept niveaux de confiance appliqués par le code avant tout outil** (propriétaire,
  équipe, partenaire, famille, ami, contact, inconnu) ; **un appel sortant exige ton « oui » au
  numéro exact et au brief exact** (parce que ton fil peut contenir du texte injecté) ; le compte
  rendu d'appel revient par webhook avec transcription, verdict et actions.

---

## 2. iMessage

### 2.1 Les trois seules voies

| Voie | Principe | Pour Orion |
|---|---|---|
| **Apple Messages for Business** | Officiel, via un prestataire agréé, revue d'Apple | ❌ Exige une société, un support humain, l'utilisateur doit écrire en premier, pas de groupes, interface « entreprise ». Seulement si Orion devient un produit. |
| **Fournisseurs commerciaux** (fermes de Mac en datacenter) | Vrais numéros + identifiants Apple, API REST + webhooks | ✅ Phase 1 pour l'identité d'Orion. Risque : Apple peut couper toute la catégorie ; le fournisseur voit les messages en clair. |
| **Auto-hébergé** | `imsg` (lecture de `chat.db` + AppleScript), BlueBubbles, réimplémentation du protocole | ✅ `imsg` sur **ton** Mac pour le contexte. ❌ Réimplémentation (rustpush/OpenBubbles) : fragile, licence SSPL, risque de bannissement. |

### 2.2 Fournisseurs comparés (prix approximatifs, à confirmer)

| Fournisseur | Prix | Points clés |
|---|---|---|
| **Inkbox** (YC S26) | Gratuit / 30 $ / 200 $ | SDK **Python**, offre gratuite suffisante pour un seul propriétaire (il écrit en premier), voix, mail, point A2A. Jeune société. |
| **Sendblue** | ~100 $/ligne/mois (offre « agent IA ») | SDK Python, groupes, réactions, indicateur de frappe, **API FaceTime Audio**. |
| **Linq** | Non public (~250 $/ligne + frais) | Le plus complet (groupes jusqu'à 31, apps iMessage), clients Poke, Lindy, peut-être Instinct. |
| LoopMessage, Blooio, Photon, Dial | 40–300 $/mois | Moins documentés ou orientés prospection. |

**Ce qui fait bannir une ligne** : volume, nouveaux destinataires, ratio envoi/réception
déséquilibré, signalements « indésirable ». Lindy a vu son identifiant Apple **banni en une
demi-journée** en auto-hébergé. Pour un seul propriétaire qui répond, le risque est faible.

### 2.3 Deux usages, deux modèles

- **Orion t'écrit** : ligne d'Orion, tu écris en premier une fois, puis il peut être proactif.
- **Orion écrit à d'autres pour toi** :
  - *(a) en tant qu'assistant, depuis son numéro* — transparent (« Bonjour, ici Orion, l'assistant
    de Noan »), limité à ~40–50 nouveaux destinataires/jour ;
  - *(b) en tant que toi, depuis ton compte via `imsg` sur ton Mac* — naturel, chiffré de bout en
    bout, mais **rayon d'action maximal** : approbation message par message au début, puis
    niveaux de confiance.

### 2.4 Leçons d'usage (Lindy, Instinct)

Indicateur de frappe plutôt que « je travaille dessus… » ; regrouper les messages entrants en rafale
(fenêtre de 3 s) ; réactions natives comme accusés de réception ; point principal dans la première
ligne ; notes vocales en entrée très utilisées (raccourci Action Button).

---

## 3. Voix et téléphonie

### 3.1 Modèles

| Option | Prix | Latence de tour | Remarques |
|---|---|---|---|
| **OpenAI GPT-Live 1** | 0,05 $/min (+ backend) | ~0,8 s (chiffre OpenAI) | **Délégation au client** : il parle, ton backend réfléchit et injecte du contexte. Seules 2 voix sans accent en français selon un utilisateur. |
| **OpenAI gpt-realtime-2.1** | ~0,15–0,20 $/min | ~1,4 s | Plus mûr, « préambules » (« je regarde… ») natifs. |
| **Gemini 3.8 Live** | ~0,02 $/min | non publiée | Appels d'outils **non bloquants** avec résultat annoncé « quand tu as fini de parler » ou en interruption si urgent. |
| Grok Voice | 0,08 $/min | ~0,8 s | En tête d'un classement (source unique). |
| **Chaîne STT → LLM → TTS** | variable | ~0,7–1,4 s | Contrôle total, n'importe quel modèle (dont Claude). STT français : Deepgram Flux, Kyutai (français, Paris). TTS : ElevenLabs, Cartesia, Gradium (Kyutai), Voxtral (Mistral). |

**Aucun comparatif indépendant de qualité en français** n'existe : il faudra un test à l'aveugle
maison (chiffres, dates, mots anglais, noms propres).

### 3.2 Passerelle et tours de parole

- **Pipecat** (Python, open source) : transports interchangeables (Twilio, WebRTC…), service
  GPT-Live avec délégation, détecteur de fin de tour **Smart Turn v3** (français inclus, 12 ms sur
  CPU). → **Meilleur choix pour Orion.** LiveKit est l'alternative, surtout si app iOS.
- Les pauses françaises (« euh… », « du coup… ») coupent les détecteurs naïfs : détecteur
  sémantique + attente plus longue si la phrase finit sur une conjonction.
- Budget : **500–800 ms** après la fin de ta phrase. **Le penseur lent n'est jamais dans la boucle
  du tour de parole.**

### 3.3 Téléphonie

- **Twilio** : numéro français ~1,35 $/mois (dossier réglementaire : pièce d'identité + justificatif
  d'adresse française, 1–2 semaines), ou numéro américain immédiat pour développer. Appels vers
  mobile français 4 c/min si émis depuis l'UE, 16 c sinon.
- **OpenAI SIP direct** : le moins de sauts, point de terminaison UE ; sortant uniquement via
  GPT-Live.
- **Coût réel pour un utilisateur : quelques dollars par mois.** On optimise la qualité, pas le
  prix.

### 3.4 Réglementation (non juridique)

- ARCEP : ne **pas** utiliser un 06/07 comme numéro d'appel d'Orion → numéro géographique 01–05 ou 09.
- **AI Act art. 50** (applicable depuis le 2 août 2026) : si Orion appelle un tiers (restaurant,
  médecin), il doit dire d'entrée qu'il est une IA. Pour toi, c'est évident.
- **Enregistrement** (Code pénal 226-1) : consentement nécessaire ; pour les tiers, annoncer ou ne
  garder que des transcriptions.

### 3.5 Architecture « bavard rapide + penseur lent »

```
iPhone ─RTC─> Twilio ─(Media Streams | SIP)─> Passerelle voix (Pipecat)
                                         │  modèle vocal (GPT-Live / Gemini Live)
                                         │  outils : consult_fast, start_task, task_status,
                                         │           cancel_task, send_imessage, end_call
                                         ▼
                           Noyau Orion (tâches, mémoire, politiques)
                            ├ chemin rapide : extraits de mémoire, brief en cache (< 300 ms)
                            └ chemin lent : modèles profonds, CLI (secondes à minutes)
```

- **Brief vocal préparé pendant la sonnerie** : qui appelle, niveau d'identité, heure, agenda en
  cours, tâches ouvertes, 3 dernières interactions tous canaux confondus. Pour un appel sortant,
  Orion prépare aussi sa **première phrase** (« Salut Noan, c'est Orion — ton vol de 18 h est
  annulé, j'ai deux options »).
- **Outils à deux vitesses** : `consult_fast` (délai max 1,5 s) ; `start_task` rend tout de suite un
  identifiant et une durée estimée, le résultat est réinjecté dans l'appel ou envoyé par iMessage.
- **Échelle d'attente** : rien → préambule (~0,7–2 s) → point d'étape (~6 s) → « tu attends ou je
  te l'envoie par iMessage ? » (~15–20 s). Jamais plus de 3 s de silence.
- **Continuité** : un seul fil de conversation pour voix et iMessage ; à la fin de l'appel, résumé
  et engagements par iMessage (« Je te dois : 1) le comparatif de vols, 2) le rappel à Paul »).
- **Sécurité par niveaux d'identité** (appliqués par le code, jamais par le modèle) :
  - L0 inconnu → prendre un message ;
  - L1 numéro autorisé + attestation, ou appel émis par Orion → **lecture seule** ;
  - L2 + code PIN tapé au clavier ou approbation Face ID par iMessage → écritures réversibles ;
  - L3 irréversible (paiement, mail à un tiers) → **jamais à la voix seule**.
  - Pas de biométrie vocale : les voix se clonent avec quelques secondes d'audio.
- Les faits dits à l'oral entrent en mémoire comme **« non confirmés »** jusqu'à confirmation écrite.

---

## 4. Proactivité et gestion de l'attention

### 4.1 Ce que dit la recherche

- **Horvitz (1999)** : agir seulement si l'utilité attendue d'agir dépasse celle de ne pas agir. **Le
  seuil dépend du coût de l'erreur** : une remarque anodine peut partir avec peu de certitude, un
  appel exige d'être presque sûr.
- **Priorities (Horvitz)** : chaque élément a un **coût du retard** ; on le compare au **coût
  d'interruption** déduit du contexte → alerter maintenant, plus tard, ou sur un autre canal. C'est
  l'ancêtre de l'échelle « résumé → message → appel ».
- **Importance ≠ interruptibilité** (Mehrotra 2016) : deux modèles séparés, combinés au moment de
  livrer.
- **Moments de rupture** (Iqbal & Bailey) : livrer à la fin d'une tâche réduit la frustration. Dans
  la vie : fin d'une réunion, arrivée quelque part, fin d'un appel, déverrouillage après longue
  inactivité.
- **Regroupement** (Fitz 2019) : **3 lots par jour** améliorent attention, humeur et stress ; toutes
  les heures = aucun effet ; tout couper = plus d'anxiété. Les gens acceptent d'attendre s'ils ont
  confiance que l'urgent passera.
- **Apple** : la règle de l'utilisateur prime toujours sur le modèle ; rien de ce qui est réduit au
  silence n'est perdu.
- **Agents proactifs LLM** : même les meilleurs modèles ne prédisent les besoins qu'à ~66 % de F1
  (ProactiveBench) → **les fausses alertes sont normales, il faut concevoir pour elles**. Préparer en
  silence ce dont tu auras besoin (ProAct : −15 % de tours, −28 % d'hallucinations) vaut souvent
  mieux qu'interrompre.

### 4.2 Le moteur d'initiative

```
Sources (mail, agenda, iMessage, position, fichiers, veilles web, minuteurs)
   → Normalisation en événements
   → Étape 1 : règles déterministes (VIP, motifs, doublons, « rien n'a changé ») — gratuit
   → Étape 2 : tri par petit modèle → {pertinence, urgence, action possible, confiance, courbe de coût du retard}
   → Étape 3 : enrichissement (mémoire, engagements, objectifs) — lecture seule
   → Arbitre d'initiative : utilité(agir) vs utilité(se taire) + budget + réglages + apprentissage
        ← modèle d'interruptibilité (téléphone, Focus, agenda, position, dernière réponse)
        ← politiques (tes règles, niveaux d'autonomie, heures calmes, « réveille-moi pour… »)
   → Décision : ignorer · préparer seulement · résumé · message au prochain moment de rupture
                · message maintenant · appel · proposer une action
   → Rédaction avec « pourquoi maintenant » + retour en un geste
   → Retours (lu, réponse, réaction, action, mise en sourdine) → apprentissage
Travailleur de nuit : consolidation mémoire, extraction d'engagements, préparation des briefs.
```

- **Échelle de livraison** : journal seulement → prochain résumé (matin, début d'après-midi, soir) →
  iMessage silencieux → iMessage au prochain moment de rupture → iMessage immédiat (puis escalade si
  non lu et toujours urgent) → **appel** (vol annulé maintenant, fraude, engagement dans moins d'une
  heure clairement pas fait, sécurité).
- **Chaque message proactif dit pourquoi** : « Parce que : ton vol AF123 avance de 2 h et tu pars dans
  40 min. »
- **Apprentissage** : bandit contextuel par (catégorie × canal × moment) + transformation des retours
  explicites en **règles lisibles** ; suivre l'**accoutumance** (un même type de message perd de la
  valeur à force).
- **Autonomie par type d'action** : chaque type commence au niveau « approbateur » ; après k
  approbations sans modification, Orion **propose** une promotion ; toute annulation le rétrograde.
  Le travail en arrière-plan est **en lecture seule par construction** (Dots).
- **Métriques** : précision des interventions, taux d'acceptation, **besoins manqués** (bouton « tu
  aurais dû me le dire »), interruptions pendant Focus/sommeil, mises en sourdine. Pas
  l'« engagement ».

---

## 5. Mémoire d'un second cerveau

### 5.1 Ce que montrent les systèmes et les benchmarks

- Un simple agent qui fouille des fichiers bruts fait aussi bien que des systèmes de mémoire dédiés
  sur LoCoMo (74 %). Les écarts de moins de ~5 points ne veulent rien dire : 6,4 % des réponses de
  référence de LoCoMo sont fausses et le juge accepte 63 % de réponses volontairement erronées.
- **Faiblesses stables partout** : raisonnement temporel, mises à jour de connaissances,
  agrégation sur plusieurs sessions (« combien de fois… »), et **savoir dire « je ne sais pas »**.
  Les fenêtres d'un million de tokens ne les résolvent pas (BEAM).
- **Convergence des produits** (ChatGPT « Dreaming V3 » en juin 2026, Gemini) : un **résumé
  modifiable et visible** + une recherche dans l'historique brut, entretenus **en arrière-plan**.
- **Zep/Graphiti** : graphe de connaissances **bi-temporel** — quand le fait était vrai dans le
  monde, et quand on l'a appris ou retiré. Un fait contredit est **invalidé, pas supprimé** : on peut
  toujours répondre à « où habitait-il en 2023 ? ».
- **Mem0** : la mise à jour standard est AJOUTER / MODIFIER / SUPPRIMER / RIEN. **Memory-R1** montre
  qu'un gestionnaire entraîné par renforcement sur **152 exemples** évite les erreurs des
  gestionnaires « promptés » (qui suppriment le premier chien quand tu en adoptes un second).
- **Honcho** : on **interroge** le modèle de l'utilisateur en langage naturel (« quel ton préfère
  Noan quand il est stressé ? ») ; chaque pair a sa propre perspective sur les autres.
- **MIRIX** : un **coffre** séparé pour les données sensibles (identifiants, adresses), jamais
  rappelé par défaut.
- **La mémoire est une surface d'attaque** : MINJA injecte de faux souvenirs avec de simples
  requêtes ; AgentPoison empoisonne la mémoire.

### 5.2 Ce dont Orion a besoin (et qu'aucun système ne fait en entier)

| Type | Contenu | Particularité |
|---|---|---|
| **Épisodique** | Événements bruts immuables (messages, mails, agenda, transactions) | Source de vérité, jamais réécrite |
| **Sémantique** | Faits, entités, relations, préférences | Dérivés, versionnés, bi-temporels |
| **Procédurale** | Comment tu aimes que les choses soient faites, savoir-faire | Jamais écrite depuis un contenu non fiable sans ta confirmation |
| **Prospective** | Engagements, promesses, échéances, déclencheurs conditionnels | **Presque personne ne la traite comme un objet de premier rang** |
| **Croyances** | Opinions et hypothèses avec confiance | Séparées des faits |
| **Coffre** | Identifiants, numéros sensibles | Jamais rappelés par défaut |

- **Personnes** comme entités résolues (alias, mails, numéros), fusions réversibles, jamais
  automatiques sur le seul prénom (« Marc » du travail ≠ « Marc » le cousin).
- **Oublier une source** (« oublie tout ce qui vient de Gmail ») = une opération qui suit la
  provenance : supprimer les épisodes, recalculer ou supprimer les faits qui ne reposaient que sur
  eux, inclure embeddings et résumés, poser des pierres tombales contre la réingestion.
- **Récupération** : hybride (mots-clés + vecteurs + graphe + filtre temporel), puis agentique pour
  les questions difficiles ; noyau toujours en contexte de ~2K tokens (identité, projets actifs,
  engagements ouverts), reconstruit chaque nuit.
- **Évaluation maison dès le premier jour** : mises à jour, questions temporelles, abstention,
  rappel des engagements, complétude de l'oubli, résistance à l'injection.

---

## 6. Collaboration entre instances d'Orion

### 6.1 Le socle technique

- **A2A v1.0** est le protocole entre agents (Linux Foundation, plus de 150 organisations, intégré à
  Azure, Bedrock, Copilot Studio ; SDK Python). Il apporte : carte d'agent **signée**, carte
  **étendue visible seulement après authentification**, cycle de vie des tâches avec
  `INPUT_REQUIRED` et `AUTH_REQUIRED`, flux, notifications poussées (pour les instances hors
  ligne). **MCP** reste pour les outils.
- **Identité** : chaque instance est un principal cryptographique (clé Ed25519, carte signée,
  `did:web`). **Délégation qui ne fait que restreindre** : jetons **Biscuit** (chaque bloc ajouté
  réduit les droits, vérifiable hors ligne), chaîne de délégation tracée.
- **Coordination entre machines** : un seul écrivain par ressource (baux + jetons de clôture),
  journaux d'événements par instance, horloges logiques hybrides, **effets externes « effectivement
  une fois »** (clé d'idempotence + outbox ; si le résultat est inconnu, demander, ne jamais
  réessayer à l'aveugle), boîtes aux lettres pour les instances absentes.
- **Contradictions conservées**, pas fusionnées en silence (StateFuse) : un conflit devient un
  objet à résoudre par celui qui détient la source faisant autorité, ou par toi.
- **Une seule instance de façade possède ton attention** : les autres ne te parlent jamais
  directement, elles lèvent `INPUT_REQUIRED` vers elle.

### 6.2 Entre personnes différentes — l'état réel

- **Aucun produit grand public** ne fait dialoguer ton agent personnel avec celui de quelqu'un
  d'autre, d'un fournisseur à l'autre (Instinct le fait, mais seulement entre utilisateurs
  d'Instinct). Les ancêtres académiques (agents de planification de réunions, 1993–1995) disaient
  déjà : garder les agendas en local pour la vie privée.
- **Ce que montrent les expériences de 2026** :
  - **Project Deal** (Anthropic) : les personnes représentées par un modèle plus fort obtiennent de
    meilleurs accords, **et les perdants ne s'en rendent pas compte** ;
  - **Agents of Chaos** : des agents réels ont **obéi à des non-propriétaires**, divulgué des
    données, usurpé des identités ;
  - **Moltbook** (réseau social d'agents) : ~2,6 % des publications contenaient des injections
    visant d'autres agents (source secondaire) ;
  - **Magentic Marketplace** (Microsoft) : les agents se laissent manipuler et favorisent la première
    proposition ;
  - **PrivacyLens-Live** : la fuite de données est bien plus forte en échanges entre agents ; un
    vérificateur de confidentialité la fait passer de ~35 % à ~7 % ; **AirGapAgent** : restreindre
    l'agent aux seules données nécessaires restaure 97 % de protection.
  - Réputation globale (ERC-8004) : dominée par des faux comptes, inutilisable.
- **Juridiquement** : la responsabilité retombe sur celui qui déploie l'agent ; l'attribution des
  contrats conclus entre agents n'est pas tranchée en droit européen.

### 6.3 Les couches qu'Orion doit construire au-dessus d'A2A

1. **Présentation** : jamais de contact à froid accepté. Échange humain (QR, lien, code comme les
   numéros de sécurité Signal), **présentation par un ami commun** (jeton signé, limité à un but et
   une durée, confiance non transitive au-delà d'un saut), ou service public vérifié. Un contact
   inconnu arrive en **quarantaine sans aucun droit**.
2. **Relation comme objet de premier rang** : identité et clés épinglées, niveau (intime, ami,
   collègue, service, inconnu), compétences autorisées, politique de divulgation, budgets,
   expiration, journal. La carte étendue vue par l'autre est **générée** depuis cet objet.
3. **Autorité entrante = capacité ; autorité sortante = mandat.** Ce que l'Orion de l'autre peut faire
   chez toi : jeton Biscuit court. Ce que ton Orion peut t'engager à faire : **mandat d'intention**
   pré-signé (« peut accepter un dîner mardi–jeudi après 19 h à moins de 5 km ») puis **mandat
   d'engagement** signé automatiquement s'il reste dans l'intention, sinon soumis à toi.
4. **Négociateur isolé** : le processus qui parle à l'extérieur ne reçoit qu'une **enveloppe de
   divulgation** préparée pour la tâche (créneaux libres sans libellés, contraintes, plafond). Il ne
   détient jamais ta mémoire : il ne peut pas divulguer ce qu'il n'a pas.
5. **Secrets sous forme de prédicats** : ton prix plancher, ta vraie disponibilité n'existent pour lui
   que comme questions oui/non avec un nombre limité d'appels. Épuiser ce quota est un signal
   d'attaque.
6. **Divulgation par intégrité contextuelle** : type d'information × destinataire × but → autoriser,
   généraliser (« occupé » plutôt que « rendez-vous chez le psy »), refuser, ou demander. Un
   vérificateur relit chaque message sortant. Tout est journalisé.
7. **Propositions typées** : schémas par domaine (créneau, prix, modification de document) ; le texte
   libre n'engage à rien ; **un accord n'existe que comme engagement co-signé**.
8. **Seul le propriétaire commande** : un message d'un autre Orion est une **demande évaluée contre la
   politique de la relation**, jamais une instruction. Règle d'architecture, pas de prompt.
9. **Jamais le « trio mortel » dans un même processus** : données privées + contenu non fiable +
   canal de sortie.
10. **Reçus mutuels** : chaque échange se termine par un résumé co-signé de ce qui a été divulgué et
    engagé, conservé des deux côtés.
11. **Mode secrétaire** : si tu es injoignable, Orion collecte, pose des options provisoires et
    répond aux questions factuelles de l'enveloppe, mais **n'engage rien** — et le dit à l'autre.
12. **Toujours se présenter** comme l'agent IA de son humain.

Premier cas d'usage le plus réaliste : **organiser un rendez-vous entre deux personnes** (créneaux
grossiers, proposition, engagement co-signé, événement d'agenda).

---

## 7. Synthèse : les idées qui feraient d'Orion autre chose

Les rapports proposent une quarantaine d'idées nouvelles. Celles qui se renforcent d'un domaine à
l'autre, et qui forment une identité cohérente pour Orion :

### 7.1 Un seul fil conducteur : la **provenance**

Chaque souvenir, tâche, message et engagement porte d'où il vient, qui l'a produit, sur quelles
preuves, avec quel niveau de confiance. Cette seule propriété rend possibles :
- **l'oubli vérifiable** (rejouer le journal sans la source et comparer : ce qui reste est une
  fuite) ;
- **la contention des injections** (« quels souvenirs, tâches et messages dépendent de ce mail
  suspect ? » → retour arrière automatique — du traçage de contacts pour les vers de prompt) ;
- **la maintenance de la vérité** (quand une preuve tombe, ce qui en dépend est réévalué
  immédiatement) ;
- **l'audit** (« quelle instance a appris quoi sur moi, et pourquoi ») ;
- **les reçus mutuels** entre personnes.

### 7.2 Le **registre des engagements**

Les promesses traitées comme une comptabilité : *je dois / on me doit*, échéance, preuve de clôture
(mail envoyé, événement créé). C'est à la fois :
- la meilleure raison pour Orion de parler en premier (proactivité) ;
- le type de mémoire le moins bien servi (mémoire) ;
- ce qu'Orion récite à la fin d'un appel (voix) ;
- ce que deux Orion co-signent (collaboration).

### 7.3 L'**attention comme ressource gouvernée**

Un budget quotidien d'interruptions, des « baux d'attention » que les instances doivent obtenir de
l'instance de façade, des messages attachés à tes **moments de rupture** plutôt qu'à des heures
fixes, un rapport hebdomadaire **« ce que j'ai failli te dire »** pour apprendre de ce qu'il a tu.

### 7.4 La **confiance comme quelque chose qui se gagne**

Autonomie par type d'action, promue seulement avec ton accord (un rituel mensuel plutôt qu'une
interruption), rétrogradée à la première annulation ; même logique pour les relations avec les
Orion des autres (confiance locale, historique d'engagements tenus).

### 7.5 La **sécurité détachée de l'agent**

Le même motif apparaît chez Instinct (moniteur d'actions), Muse (Sentinel), Dots (vérificateur hors
de portée) et dans la recherche entre personnes (négociateur isolé, secrets en prédicats) : **ce qui
protège ne doit pas pouvoir être modifié ni convaincu par ce qui agit.**

---

## 8. Décisions proposées (à valider)

| # | Proposition |
|---|---|
| P1 | **Canal iMessage phase 1** : ligne gérée par un fournisseur (Inkbox gratuit pour démarrer, Sendblue si besoin de FaceTime) + `imsg` en lecture seule sur ton Mac. Envoi « en ton nom » plus tard, sous approbation. |
| P2 | **Voix phase 1** : Twilio + Pipecat + GPT-Live 1 (Gemini 3.8 Live en comparaison), numéro américain pour développer puis numéro géographique français. Niveaux d'identité L0–L3 appliqués par le code. |
| P3 | **Mémoire** : journal d'épisodes immuable comme source de vérité, faits bi-temporels avec provenance, Markdown lisible pour le profil et les personnes, SQLite (plein texte + vecteurs + graphe) comme index, registre des engagements de premier rang. |
| P4 | **Proactivité** : moteur d'initiative dédié (règles → petit modèle → arbitre d'utilité), 3 résumés par jour par défaut, échelle jusqu'à l'appel, « pourquoi maintenant » obligatoire, arrière-plan en lecture seule. |
| P5 | **Collaboration** : A2A v1.0 + identité par instance + jetons Biscuit ; entre personnes : relations comme objets, négociateur isolé, propositions typées, engagements co-signés. Premier cas : organiser un rendez-vous. |
| P6 | **Sécurité** : un gardien séparé de l'agent (pare-feu entrant, moniteur d'actions, politiques), jamais le trio mortel dans un même processus. |

---

## Sources principales

**Instinct et iMessage**
- https://www.forbes.com/sites/iainmartin/2026/08/26/vcs-are-so-obsessed-with-this-ai-assistant-that-its-valuation-jumped-fivefold-in-weeks/
- https://colossus.com/episode/instinct-the-personal-agent/
- https://github.com/mariagorskikh/open-instinct (dont `docs/research/INSTINCT.md`, `docs/INKBOX.md`)
- https://techcrunch.com/2026/08/24/instincts-powerful-ai-assistant-is-raising-privacy-and-security-concerns/
- https://techcrunch.com/2026/06/04/apple-approves-poke-as-the-first-ai-agent-on-its-messages-for-business-platform/
- https://www.lindy.ai/blog/imessage-api-three-rewrites-one-apple-ban-and-what-actually-works
- https://docs.sendblue.com/calling/facetime/ · https://github.com/openclaw/imsg

**Voix**
- https://developers.openai.com/api/docs/models/gpt-live-1 · https://developers.openai.com/api/docs/guides/realtime-sip
- https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live
- https://docs.pipecat.ai/api-reference/server/services/s2s/openai-live · https://github.com/pipecat-ai/smart-turn
- https://www.twilio.com/en-us/guidelines/fr/regulatory
- https://www.arcep.fr/actualites/actualites-et-communiques/detail/n/protection-des-consommateurs-290126.html

**Proactivité**
- Horvitz 1999 : https://erichorvitz.com/chi99horvitz.pdf · Priorities : https://arxiv.org/abs/1301.6707
- Iqbal & Bailey 2008 : https://www.interruptions.net/literature/Iqbal-CHI08.pdf
- ProactiveBench : https://arxiv.org/abs/2410.12361 · ProAgentBench : https://arxiv.org/abs/2602.04482
- Sleep-time compute : https://letta.com/blog/sleep-time-compute
- Niveaux d'autonomie : https://arxiv.org/abs/2506.12469

**Mémoire**
- LongMemEval : https://arxiv.org/abs/2410.10813 · BEAM : https://arxiv.org/abs/2510.27246 · MemoryAgentBench : https://arxiv.org/abs/2507.05257
- Zep : https://huggingface.co/papers/2501.13956 · Memory-R1 : https://arxiv.org/abs/2508.19828 · Hindsight : https://arxiv.org/abs/2512.12818
- Letta (système de fichiers) : https://www.letta.com/blog/benchmarking-ai-agent-memory
- Collaborative Memory : https://arxiv.org/abs/2505.18279 · MINJA : https://arxiv.org/abs/2503.03704

**Collaboration**
- A2A : https://a2a-protocol.org/latest/specification/
- https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year
- Project Deal : https://www.anthropic.com/features/project-deal · Agents of Chaos : https://arxiv.org/abs/2602.20021
- Magentic Marketplace : https://arxiv.org/abs/2510.25779 · PrivacyLens-Live : https://arxiv.org/abs/2509.17488 · AirGapAgent : https://arxiv.org/abs/2405.05175
- Morris II : https://arxiv.org/abs/2403.02817 · AIP (Biscuit) : https://www.ietf.org/archive/id/draft-prakash-aip-01.html
- https://datatracker.ietf.org/doc/html/draft-ietf-wimse-aims
