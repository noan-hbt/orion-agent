# Audit d'Orion : la distance au Jarvis (05/11/2026)

Le propriétaire veut un Jarvis : secrétaire, assistant, expert, coach, qui gère sa vie, la lui
améliore et a de l'avance sur lui. Ce qu'il constate : un peu d'autonomie, des outils appelés
quand il faut, une proactivité réduite, peu de curiosité, pas le couteau suisse attendu.

La refonte du 27/10 (`05-refonte.md`) avait posé le bon diagnostic et livré ses six chantiers.
Cet audit regarde ce qui se passe **en vrai depuis**, avec le journal de production (14 jours,
51 000 événements), la configuration du serveur et le code de `main` (`df1b7bd`).

## 0. Verdict

Orion a l'ossature d'un Jarvis : journal, gardien, outbox, veille, nuit, situations, 50 modules,
200 outils, appels téléphoniques, navigateur, coffre. Ce qui lui manque n'est pas d'abord du code.
C'est :

1. **Un cerveau à la hauteur, là où il pense.** La veille, la réflexion, la curiosité, le
   « modèle fort » et l'extraction de mémoire tournent sur le plus petit modèle du forfait.
2. **Une proactivité qui produit du travail, pas des messages.** Le code filtre et le modèle
   rédige. Ce qui passe est surtout du réactif déguisé (un mail est arrivé, Orion a redémarré).
3. **Une boucle qui vérifie ce qu'elle affirme.** « C'est vérifié » ne coûte rien à dire, et
   personne ne le contrôle.
4. **Un modèle de sa vie, pas une liste de faits.** Il n'y a ni objectifs ni projets avec jalons,
   aucun domaine (argent, santé, routines), aucune mémoire des procédures.
5. **Moins de rustines.** Chaque raté reçoit une règle, une relance ou une décision : 116 commits
   et une centaine de décisions en 9 jours. Le diagnostic 2.1 de la refonte (« les consignes
   s'empilent, le modèle s'y noie ») est toujours vrai, il s'est juste déplacé dans les relances
   injectées et le manuel appris.

## 1. Ce qui a été mesuré (22/10 → 05/11)

| Mesure | Valeur |
|---|---|
| Messages du propriétaire | 1 238 (17 à 277 par jour) |
| Tours conversationnels | 1 213 ; réflexion médiane 12 s, p90 63 s, max 26 min ; 127 tours de plus de 60 s |
| Étapes par tour | médiane 2, p90 7, max 42 |
| Appels modèle | 6 239 : `gpt-6-luna` 59 %, `gpt-6.1-terra` 17 %, `gpt-6.1-sol` 9 %, Haiku 8 %, Opus 0,7 % |
| Jetons de prompt par appel | médiane 70 k, p90 196 k, max 785 k ; **605 M au total** |
| Initiatives déclenchées / livrées / tues | 588 / 242 / 231 |
| Livrées, par source | mail 49, veille 29, points 28, rappels 26, lieux 21, missions 15, redémarrage 13, tâches 10, conseils 9, **pensée 7, curiosité 1, intention 1, anniversaire 1** |
| Tues, par source | **redémarrage 97**, mail 52, lieux 31, points 19 |
| Ratés relevés (`adaptation.missed`) | 89, soit 6 par jour |
| Erreurs avouées (`mistake`) | 22 |
| Faits affirmés / invalidés | 275 / 142 (la moitié de ce qu'il apprend est corrigé ensuite) |
| Règles du manuel apprises / retirées | 195 / 54 |
| Erreurs d'outils | `web_read` 146 sur 715 (20 %), `browser` 130 sur 1 181 (11 %) |
| Coût facturé | 3,63 $ (le reste sur forfaits) ; quota ChatGPT épuisé le 05/11 à 19 h 06 |

Ce que ces chiffres disent :
- **L'anticipation pure (pensée, curiosité, intention, anniversaire) fait 10 messages sur 242.**
  Le reste répond à un événement extérieur. C'est la mesure exacte de « pas de longueur d'avance ».
- Il y a eu **110 initiatives de redémarrage** en 14 jours : Orion redémarre environ 8 fois par jour
  (déploiement automatique toutes les 2 min, sur chaque merge). Chaque redémarrage ferme le
  navigateur et perd l'état en mémoire (prises en main, onglets) : d'où « No page is open » le
  05/11.
- **6 ratés par jour** relevés, et c'est un plancher : il ne voit que ce que le propriétaire
  corrige explicitement.

## 2. Causes racines, par ordre d'impact

### C1. Le cerveau est sous-dimensionné là où Orion doit penser

Configuration de production (`/srv/orion/orion.toml`) :

```
model            = gpt-6.1-sol    # la conversation
strong_model     = gpt-6-luna     # ← le « modèle fort » est le plus petit
extractor_model  = gpt-6-luna     # ce qu'il retient de toi
[models.watch]      gpt-6-luna    # la veille, 3 fois par jour : l'intuition, le recul
[models.reflection] gpt-6-luna    # ce qu'il apprend de ses ratés
[models.curiosity]  gpt-6-luna
[models.portrait]   gpt-6-luna
[models.tasks]      gpt-6.1-terra
verifier_model   = ""
```

La refonte avait conçu la veille comme « un passage de réflexion avec le modèle fort qui regarde
l'ensemble » (chantier 2). En prod, elle tourne sur le plus petit modèle. La conversation est sur
`sol`, mais dès que le quota du forfait tombe (le 05/11 à 19 h), tout bascule sur `luna`.
Un petit modèle sait suivre une consigne. Il ne relie pas « passeport en décembre » à « Lisbonne
vendredi », et ne se demande pas ce qui va coincer la semaine prochaine. **C'est la cause n° 1 du
manque de curiosité et d'intuition**, et elle tient à un fichier de configuration.

Le fournisseur `claude_code` (abonnement Claude, Opus 5.5) existe déjà et sert à 0,7 % des appels.

### C2. La proactivité est filtrée par construction, et ce qui passe est surtout réactif

- Le code décide seul s'il parle (`initiative/engine.py:17-24`, portail `_gate` l.1068, arbitre
  `arbiter.py:73-103`). Le modèle ne fait que rédiger, ou répondre `SILENCE`. Les prompts de
  silence sont partout : `engine.py:193,748,830,1180`, `watch/module.py:103-107` (« le plus
  souvent : ne parle que si ça l'aide »), `curiosity/module.py:490`, `awaiting`, `departure`,
  `places`, `relations`, `hooks`.
- La curiosité tourne **une fois par semaine**, le samedi à 6 h, avec 3 recherches et
  l'importance 0,25 (`curiosity/module.py:68-127`). Ses trouvailles attendent donc le prochain
  point. Un Jarvis est curieux tous les jours, et au service de ses projets.
- La veille est plafonnée à 3 passages par jour, 2 h d'écart et 0,6 $ (`watch/module.py:118-136`).
  Son rendu est un message (`MAINTENANT :` / `AU POINT :` / `SILENCE`), pas un travail.
- L'arbitre est à règles fixes : il n'apprend pas quand le propriétaire est content d'être
  interrompu. Le seul retour, c'est l'arrêt après 4 ignorances.
- **Il n'existe pas de « produit » proactif quotidien** : un briefing du matin préparé (journée,
  trajets, ce qui coince, ce qu'il a déjà réglé), une revue de la semaine, des objectifs.
  Les points de 12 h 30 / 16 h / 19 h / 22 h ne sont qu'une liste d'alertes à trier.

### C3. Aucune boucle ne vérifie ce qu'Orion affirme

- `cognition/verifier.py` ne contrôle que les initiatives (`engine.py:271,309`). **Les réponses
  au propriétaire ne sont jamais vérifiées.**
- Le seul contrôle de fin de tour, `finish.py`, cherche par regex dans les 280 derniers
  caractères (« tu veux que je… », « bien sûr ! »), une fois par tour.
- L'outil `plan` est désactivé (D235). Aucune planification explicite.
- En vrai, le 05/11 : « oui, c'est cette rubrique vérifiée » sans avoir suivi le chemin du mail
  (INPI, 9 h 14) ; « connexion Nibelis rejetée par le coffre » relayé dans un point alors que la
  connexion avait réussi ; le 31/10, « pas moi » à propos d'un accès Cyclades sans lire les logs.
  Les erreurs avouées (`mistake`) disent presque toutes la même chose : **affirmer sans preuve,
  généraliser un résultat partiel.**

Tant que dire « vérifié » ne coûte rien, aucune consigne ne réglera le problème. Il faut que le
code le rende coûteux.

### C4. Le contexte grossit sans limite à l'intérieur d'un tour

- La fenêtre n'est recalculée qu'une fois par tour, avant la boucle (`runtime.py:584-612`). Chaque
  étape renvoie tout l'historique avec les pages lues (`cognition/loop.py:340-400`). Médiane
  70 k, p90 196 k, max 785 k jetons par appel.
- Effets : les quotas des forfaits fondent (605 M de jetons en 14 jours), les tours longs
  deviennent lents (127 de plus d'une minute), et surtout le petit modèle se noie. Les pages web
  repoussent les consignes et la mémoire loin derrière.
- `max_steps = 100` et `max_tool_calls = 250` en prod : un tour qui s'égare peut aller très loin.

### C5. La mémoire est une liste de faits, pas un modèle de sa vie

- `<souvenirs>` = 6 faits, cherchés **sur le seul dernier message** (`runtime.py:214,440,1109`),
  en plein texte. « Ok mais regarde le contenu du mail » ne ramène rien d'utile.
- `<memoire>` est tronquée en silence à 10 000 caractères (`config.py:312`), et injectée
  seulement en début de fenêtre.
- Les engagements en retard restent affichés indéfiniment : le test `due <= soon` n'a pas de
  borne basse (`runtime.py:1030`). D'où « à faire pour le 2026-11-03 » dans chaque message du
  05/11.
- L'extracteur (confiance SYSTEM) peut invalider un fait dit par le propriétaire sans contrôle de
  code (`memory/service.py`, `_may_change`). Avec 142 invalidations pour 275 faits, la mémoire est
  instable.
- **Rien ne modélise** ses objectifs, ses projets avec jalons, ses domaines (argent, santé,
  études, travail, relations), ses routines. Rien ne retient non plus **comment se fait une
  démarche** : où se connecter sur Nibelis, le chemin des brouillons INPI, Hyperplanning. Orion
  réapprend chaque fois, et chaque fois se trompe de la même façon.

### C6. L'amélioration se fait par empilement

- 116 commits et une centaine de décisions (D149 → D248) en 9 jours, chacun déployé et
  redémarré en prod dans les 2 minutes.
- 9 relances injectées dans la boucle (`[Orion · ta réponse est vide…]`, `[Orion · cet outil
  vient d'échouer deux fois…]`, `[Orion · Ta réponse n'est pas encore partie…]`…). Chacune
  corrige un travers et ajoute un appel. Le 05/11, deux se sont déclenchées pour rien après
  l'envoi d'un fichier.
- 195 règles apprises dans le manuel en 14 jours, injectées dans `<memoire>`, `<manuel>` et
  `<consignes>`.
- 83 cas d'évaluation, mais **les 89 ratés réels ne deviennent pas des cas automatiquement**.

Chaque correctif est raisonnable. Leur somme fait un système où le modèle lit toujours plus de
rustines, et où rien ne garantit que le correctif d'hier tient encore.

### C7. Capacités et fiabilité des mains

- Absents : banque et budget, santé (sommeil, sport), trajets en temps réel, météo, musique,
  courses, paiements. Partiels : domotique (MCP), école (Hyperplanning par le navigateur
  seulement), voyages (SNCF par le navigateur).
- Le web casse souvent : 20 % d'échecs sur `web_read`, 11 % sur `browser`. Les anti-robots
  (DataDome) bloquent même après la prise en main (Kiabi, le 05/11), alors que l'outil promet
  le contraire.

## 3. Risques à connaître

- **Formulaires envoyés sans accord** : `browser_click`, `browser_type` (Entrée) et `browser_fill`
  ont l'effet NONE (`computer/direct.py:1243`). Seul `browser_upload` est EXTERNAL. C'est voulu
  (D115, « il navigue comme une personne »), mais un achat ou un message envoyé par un formulaire
  ne passe pas par le gardien. Pour un Jarvis qui achète et réserve, il faut un garde sur les
  pages de paiement et de confirmation.
- **Rappels explicites et hooks** passent sans portail (`engine.py:621-627`), donc la nuit et
  hors budget. C'est voulu pour un rappel. Pour un hook à `then`, ça se discute.
- **Quotas des forfaits** : un seul fournisseur porte presque tout. Quand le quota tombe, la
  qualité tombe avec lui, sans que le propriétaire le sache.

## 4. La cible : de l'assistant réactif au chef de cabinet

Un Jarvis n'est pas un chatbot qui parle plus souvent. C'est un **chef de cabinet** : il tient
l'état de ta vie, sait ce que tu veux en faire, travaille dessus en continu, et ne te parle que
pour te donner un résultat ou te demander une décision.

```
           ┌──────────────── Modèle de sa vie ─────────────────┐
           │ domaines · objectifs · projets/jalons · routines   │
           │ situations · engagements · procédures apprises     │
           └───────────────▲─────────────────────┬─────────────┘
                           │ met à jour          │ lit
   événements ──► Perception ──► Délibération (modèle fort) ──► Agenda de travail
   (mail, agenda,            veille continue : « qu'est-ce     faire seul · préparer ·
   lieux, web, lui)          qui compte, coince, se prépare ?» demander · dire
                                                               │
                                     Exécution (modèle rapide, outils, tâches)
                                                               │
                                     Vérification (preuves exigées par le code)
                                                               │
                                     Livraison (briefing, point, alerte, rien)
                                                               │
                                     Retour (ratés, oui/non, ignoré) ──► évals + arbitre
```

Ce qui change par rapport à aujourd'hui :
1. **La veille produit du travail.** Son rendu est un agenda d'actions (« je réserve », « je
   prépare le dossier », « je lui demande »), exécuté en silence. Le message vient après, avec le
   résultat.
2. **Deux étages de cognition.** Délibérer (veille, nuit, plans, curiosité, mémoire) se fait sur
   le meilleur modèle disponible. Exécuter (étapes d'outils, extraction de pages) se fait sur un
   modèle rapide.
3. **La preuve avant la parole.** Toute affirmation d'état (« fait », « envoyé », « vérifié »,
   « aucun X », « rejeté ») doit citer un résultat d'outil de ce tour. Le code le contrôle.
4. **Un modèle de sa vie** qui porte des objectifs, pas seulement des faits. La curiosité, le
   coaching et l'expertise s'accrochent à ces objectifs.
5. **Des procédures apprises** : chaque démarche réussie (site, chemin, pièges) est gardée et
   rejouée.

## 5. Plan priorisé

Chaque point se mesure avec les indicateurs du §6.

### P0 — cette semaine (configuration et correctifs ciblés, gros effet)

1. **Router le cerveau** : `strong_model`, `[models.watch]`, `reflection`, `curiosity`, `portrait`
   et `memory` passent sur le meilleur modèle disponible : `claude-code:opus` (forfait Claude),
   avec `gpt-6.1-sol` en repli. Le petit modèle reste pour le résumé et la vision. Il faut
   surveiller les quotas : `plan.quota` existe déjà.
2. **Prévenir quand le cerveau baisse** : quand `router.served_by_fallback` dure (quota épuisé),
   l'outil `orion` le dit, et la veille le signale au prochain point.
3. **Engagements en retard** : passé l'échéance, ils sortent de `<maintenant>`. Ils deviennent une
   question unique (« fait ? reporté ? abandonné ? »), puis sont clos ou reprogrammés
   (`runtime.py:1030`).
4. **Déploiements groupés** : pas de redémarrage pendant une conversation ou une prise en main.
   Déploiement au plus toutes les heures, ou la nuit, sauf correctif urgent. On tombe de
   8 redémarrages par jour à 1 ou 2.
5. **Merger la PR #178** (login Nibelis) et corriger la promesse de l'anti-robot après une prise
   en main.

### P1 — 2 à 3 semaines (les changements de structure)

6. **Vérification des affirmations** (C3) : un registre de preuves par tour. Chaque résultat
   d'outil reçoit un identifiant. Une réponse qui affirme un état sans en citer un repasse une fois
   au modèle, comme `finish_check`, mais sur le fond, pas par regex. Le `verifier` existant juge
   les réponses factuelles (dates, montants, « aucun », « vérifié »). Évals : les cas INPI et
   Nibelis du 05/11.
7. **Budget de contexte par étape** (C4) : au-delà d'un seuil (40 k ?), les résultats d'outils du
   tour courant sont résumés sur place, pas seulement après une pause. Plafond de prod ramené à
   40 étapes. Objectif : p90 sous 60 k jetons.
8. **La veille qui travaille** (C2) : elle rend une liste d'actions typées (faire seul, préparer,
   demander, dire), exécutées en tâches de fond. Le point ne présente que les résultats et les
   décisions à prendre. Plafond de 3 passages relevé, déclenchée aussi par ses messages.
9. **Briefing du matin** : un vrai produit, préparé à l'heure de son réveil. Journée, trajets et
   perturbations, ce qui coince dans les 48 h, ce qu'Orion a réglé cette nuit, une décision à
   prendre au plus. Plus une revue du dimanche : semaine passée, semaine qui vient, objectifs.
10. **Mémoire des procédures** (C5) : objet `procedure` (site ou démarche, chemin, identifiant du
    coffre, pièges, dernière réussite), écrit après une réussite vérifiée, injecté quand le site
    ou la démarche revient. La PR #178 en est le premier cas.
11. **Récupération sur le contexte** : `<souvenirs>` cherche sur les 3 derniers messages, les
    situations en cours et les entités nommées, pas seulement sur le dernier message.
12. **Ratés → évals** : chaque `adaptation.missed` devient un brouillon de cas
    (`orion evals export`), relu chaque semaine. Les évals tournent sur le modèle de prod, pas
    seulement sur Haiku.

### P2 — le mois (le modèle de sa vie et les capacités)

13. **Objectifs et projets** : objet `goal` (domaine, but, échéance, jalons, situations et
    engagements liés, bilan hebdo). La curiosité devient quotidienne et s'accroche aux objectifs :
    trouver, comparer, préparer. Le coach s'appuie dessus.
14. **Domaines structurés, consentis et dans le gardien** : argent (GoCardless en lecture,
    abonnements, fin de mois), santé (Raccourcis iPhone : sommeil, pas, sport), trajets (PRIM
    IDFM / SNCF), météo.
15. **Arbitre qui apprend** : chaque initiative reçoit un retour (réponse, ignorée, « merci »,
    « pas maintenant »), et l'importance par source s'ajuste sur 30 jours.
16. **Garde des achats** : une page de paiement ou de confirmation détectée fait passer le clic
    final par le gardien, sauf autonomie accordée pour ce site.
17. **Élaguer** : supprimer les relances `[Orion · …]` devenues inutiles une fois la vérification
    (6) et le budget de contexte (7) en place. Fusionner les règles du manuel qui se recouvrent.
    Objectif : moins de 60 règles actives.

## 6. Indicateurs à suivre (`orion week`)

| Indicateur | Aujourd'hui | Cible à 1 mois |
|---|---|---|
| Messages d'anticipation utiles par jour (pensée, curiosité, objectifs, préparation) | 0,7 | 3 |
| Travail fait en silence par jour (tâches lancées par la veille, closes avec résultat) | ~1 | 5 |
| Ratés relevés par semaine | 42 | < 15 |
| Affirmations sans preuve (nouvel indicateur, registre du P1-6) | non mesuré | 0 |
| Tours de plus de 60 s | 10 % | < 4 % |
| Jetons de prompt par appel, p90 | 196 k | < 60 k |
| Erreurs `web_read` / `browser` | 20 % / 11 % | < 10 % / < 6 % |
| Redémarrages par jour | ~8 | ≤ 2 |
| Part des appels de délibération sur un modèle fort | ~0 % | 100 % |

## 7. Ce qui ne change pas

Le journal en ajout seul, les projections reconstruites, la provenance et la confiance, le
gardien, l'outbox, le contrat du cache de prompt. Ce sont eux qui permettent de donner plus
d'autonomie sans perdre le contrôle. Le Jarvis se construit dessus.
