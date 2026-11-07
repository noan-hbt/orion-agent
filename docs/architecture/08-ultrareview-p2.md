# Ultrareview du 05/11 — chantier P2

Plan de reprise des travaux P2 de l'audit `docs/audit_orion_ultrareview_2026-11-05.pdf`
(registre §13, ORI-014 à ORI-023, plus les constats F-06, F-08, F-09), confronté au code de
`main` au 07/11/2026 (HEAD `8ad91fb`, jusqu'à D270), puis revérifié sur l'historique jusqu'à
`9bcb02d` (D275) : aucun point P2 n'y est livré ; D271, D273 et D274 changent des appuis
(voir F-09, ORI-015, ORI-020). Les numéros de ligne sont indicatifs : ils
dérivent, relire le code avant d'éditer. Le P1 est dans `07-ultrareview-p1.md`.

Note : le tableau §13 du PDF a des colonnes décalées. Correspondance retenue : ORI-014 attente de
l'arbitre, 015 curiosité ResearchLead, 016 rappel dirigé par objectifs, 017 artefacts, 018
provenance du portrait, 019 registre des capacités, 020 routeur, 021 messages structurés, 022
permissions, 023 routines.

## Comment reprendre

- Un point = un ticket Jira `ORION-<n>` = une branche = une PR (règles dans `CLAUDE.md`). Jira
  n'a aucun ticket par point ORI-0xx ; l'épopée proche est **ORION-2** (Backlog, label
  `audit-2026-11-05`). Créer les tickets en Backlog, liés à ORION-2.
- **Les choix sont tranchés** (07/11, délégués par le propriétaire) : voir « Décidé » dans
  chaque point. Chaque PR qui en applique un l'inscrit dans `docs/DECISIONS.md` (nouveau Dxxx,
  qui cite le Dxxx qu'il précise).
- Ordre conseillé : ORI-014 → F-09 → ORI-018 → ORI-016 → ORI-017 → ORI-019 → ORI-015 →
  ORI-022 → ORI-020 → ORI-021 → ORI-023.

| Point | État | Décision |
|---|---|---|
| ORI-014 attente arbitre (F-06) | absent | à coder |
| F-09 vérification découpée | partiel | à coder (réutiliser `grounding.py`) |
| ORI-018 provenance du portrait | absent | à coder |
| ORI-016 rappel par objectifs | partiel | à coder |
| ORI-017 artefacts (F-08) | absent | contenu dans le journal |
| ORI-019 registre des capacités | partiel | ticket à part, sous ORION-2 |
| ORI-015 curiosité ResearchLead | largement fait (D268) | D268 tient ; journaliser la piste |
| ORI-022 permissions et effets | partiel | expiration optionnelle, aucune par défaut |
| ORI-020 routeur coût/risque | partiel | mesurer d'abord, pas de routage par risque |
| ORI-021 messages structurés | absent | D126 tient ; métadonnées seulement |
| ORI-023 routines | absent | seulement sur routine déclarée par le propriétaire |

---

## ORI-014 — Persister l'attente de l'arbitre (F-06)

**Audit.** `_waiting` vit en mémoire : après un redémarrage, une candidature perd son ancienneté,
donc son urgence. Journaliser `first_seen_at`, `last_evaluated_at`, `last_reason`,
`defer_until`, `expires_at` ; révision de priorité idempotente.

**Existant.** `initiative/engine.py` : `self._waiting: dict[str, datetime]` (~l. 686), rempli par
`setdefault` (~1105) et vidé (~1092, ~1107) dans `_admit` (~1087-1108), qui passe
`self._waiting.get(key)` à `arbiter.judge(...)` (`initiative/arbiter.py` ~73) ;
`waiting_since` ajoute `min(0.4, 0.1 * heures)` à l'urgence (`arbiter.py` ~88-90).
`expires_at` déjà géré (`engine.py` ~749, ~1231). Contexte : D144/D146 (arbitre), D269 (retours
par source).

**Manque.** L'ancienneté n'est ni journalisée ni reconstruite au démarrage.

**Plan.**
- Journaliser `initiative.waiting` (key, first_seen, reason, expires_at) au premier
  `setdefault` seulement, et `initiative.waiting_cleared` (key, why) au retrait.
- Petite projection `WaitBook` (dans `initiative/`, reconstruite du journal) qui alimente
  `_waiting` au démarrage. Pas de nouveau package.
- Test : une candidature en attente depuis 2 h a la même urgence après reconstruction du moteur
  (horloge simulée).

**Fait quand.** `judge()` reçoit le même `waiting_since` avant et après un redémarrage.

---

## F-09 — Vérification de la sortie proactive, découpée

**Audit.** Dissocier `factual_grounding`, `policy_check`, `outcome_verification`,
`relevance_check` ; contrôle déterministe quand c'est possible (« j'ai réservé » exige un
identifiant de réservation ou une confirmation du fournisseur).

**Existant.** `cognition/verifier.py` : `Verifier.check(draft, facts, conversation)` (~102),
second modèle, échec fermé, `_Verdict`, `Verification`, `settled`/`corrected`/`_per_doubt`.
`initiative/engine.py` ~1287-1370 vérifie avant envoi et réécrit une fois. Contrôle déterministe
des affirmations d'état : `cognition/grounding.py` (D252, D258). `cognition/finish.py` : regex de
fin de réponse, messages du propriétaire seulement. D270 : les relances `[Orion · …]` ne sont
pas relues.

**Manque.** Un seul appel LLM mêle faits et pertinence ; pas de contrôle de politique, ni de
pertinence liée à un objectif, ni de vérification déterministe d'issue.

**Plan.**
- Type `Check(kind, ok, reason)` ; la vérification renvoie une liste de `Check`, l'initiative
  n'envoie que si tous les contrôles requis passent.
- `outcome` : déterministe, réutiliser `grounding.py` et `tools/outcome.confirmed_proof` (D274,
  la seule règle de preuve, déjà lue par objectifs et tâches ; pas de doublon) : une affirmation
  d'effet sans action `CONFIRMED` est bloquée sans appel modèle.
- `relevance` : état de l'objectif lié (`goals`), déterministe d'abord.
- `factual` : le `Verifier` actuel. `policy` : le gardien, s'il y a une action.
- Journaliser chaque `Check` dans l'événement de décision existant.
- Test : un message proactif qui affirme une réservation sans identifiant est bloqué, sans appel
  modèle.

**Fait quand.** Chaque type de contrôle a une issue journalisée par message proactif.
Lien : ORION-24 (Backlog, relecture à tort).

---

## ORI-018 — Provenance par affirmation du portrait

**Audit.** Le portrait est une vue ; chaque fait important renvoie à ses sources
(`source_event_ids`, `valid_at`, `confidence`, `status`, `challenged_by`) ; « pourquoi le
crois-tu ? » répond avec les messages d'origine.

**Existant.** `memory/portrait.py` : `write_portrait` (~110-175), `PROMPT` (~45-61), texte libre
tronqué à `MAX_CHARS`, événement `memory.portrait.written` `{text, day, facts}` (`facts` = un
compte). `Portrait.apply`, `portrait_block`. Les faits ont `source`, `event_id`, `trust`,
`known_from_seq` (`memory/store.py` ~144-160). D144. Le portrait est déjà réécrit après l'oubli
d'une source.

**Plan.**
- Le modèle rend des affirmations `{text, fact_ids}` (sortie structurée).
- **Le code valide** que chaque `fact_id` est dans la liste envoyée ; sinon l'affirmation est
  écartée (échec fermé).
- Ajouter `claims: [{text, fact_ids, source_event_ids}]` au payload, garder `text` pour le rendu
  et la compatibilité des projections existantes (vérifier tous les lecteurs de `text`).
- Tests : toute affirmation journalisée pointe vers des faits fournis ; après l'oubli d'une
  source, l'affirmation disparaît au portrait suivant.

**Fait quand.** Les sources d'une affirmation du portrait se lisent depuis le journal.

---

## ORI-016 — Rappel dirigé par les objectifs

**Audit.** Rappel guidé par l'intention : contraintes et personnes qui touchent le plan,
changements depuis la dernière décision, exceptions. Mesurer le rappel des informations
critiques, pas seulement la précision.

**Existant.** `runtime.py` ~1267-1280 : `self._memory.search(text, self._recall_limit)` et
`_follow_up_query` (ORION-4, commit `bca613e`, relire sa décision). `memory/semantic.py` :
`search(text, limit, include_past)` (~116), RRF FTS5 + embeddings, `MIN_SIMILARITY = 0.40`.
Rendu par `memory/render.py` `recall_block`.

**Plan.**
- `search(..., *, intent=None)` : `intent` = titres et `done_when` des objectifs actifs,
  situations ouvertes, engagements ouverts. Second passage de recherche sur l'intention,
  fusionné par RRF ; pas de nouveau seuil.
- Le résultat reste dans le bloc `<memoire>` du message courant, jamais dans le message système
  (contrat de cache, `tests/test_runtime_cache.py`).
- Cas d'éval `evals` : « contrainte oubliée » (une contrainte liée à un objectif, sans
  recouvrement lexical avec la question).
- Test : cette contrainte remonte pour la requête.

**Fait quand.** Le cas d'éval passe sur le modèle réel.

---

## ORI-017 — Artefacts volumineux navigables (F-08)

**Audit.** Gros résultats stockés comme artefacts immuables (id, hash, provenance,
lignes/sections) ; le modèle reçoit un aperçu, un index et des identifiants de fragments ; outil
de lecture par plage et de recherche ; les conclusions citent les fragments lus.

**Existant.** `cognition/loop.py` `_clip` (~1010-1022) : garde 2/3 début, 1/3 fin, note
« [… N characters cut here …] ». Limite par outil : `tools/base.py` `max_output_chars = 20_000`.
Rien d'autre. D231 (résultat d'une tâche vu en entier) à relire.

**Décidé.** Le contenu entier va **dans le journal**, payload de `tool.artifact`. Raisons : la
règle « tout se reconstruit depuis le journal seul » et `redact` le couvrent sans code nouveau ;
un fichier à part serait un second stockage à sauvegarder et à effacer. Plafond par artefact
(1 Mo) : au-delà, on garde le premier Mo et l'événement le dit (`truncated: true`), jamais en
silence.

**Plan.**
- Quand `_clip` coupe : journaliser `tool.artifact` (artifact_id, sha256, taille, lignes,
  provenance, trust de la sortie d'origine) ; l'aperçu se termine par l'id et le moyen de lire
  la suite.
- Outil `artifact` (`read(id, start, end)`, `search(id, query)`), présent dans le registre fixe
  et trié dès le démarrage (contrat de cache). Trust de la lecture = trust de l'artefact.
- Test : une sortie de 100 000 caractères avec une aiguille au milieu est retrouvée par
  `artifact.search`.

**Fait quand.** Aucune coupure sans moyen de relire ; la coupure nomme l'artefact.

---

## ORI-019 — Registre des capacités et santé des connecteurs

**Audit.** Montrer ce qu'Orion sait finir avec garantie, ce qu'il tente, ce qu'il ne sait pas
faire ; couvertures et pannes visibles.

**Existant.** `modules/base.py` (`Module`, `enabled()`, `doctor()`), `cli.py` `_doctor` (~891) ;
`tools/base.py`, `modules/tools.py`. D24, D264 (un hook non fait se dit une fois),
D153/D159-D161. Chantier 6 de `05-refonte.md`.

**Décidé.** ORION-2 est l'épopée P2 : ORI-019 y devient un ticket à part, enfant d'ORION-2.
Il réutilise `doctor()` des modules au lieu d'une seconde sonde.

**Plan.**
- Méthode optionnelle `capabilities()` sur `Module` ; module `orion/modules/capabilities.py`
  qui sonde périodiquement (`doctor()`) et n'émet `capability.health_changed` qu'à une
  transition.
- Outil en lecture seule `capabilities` dans le registre trié ; rien dans le message système.
- Test : un connecteur qui tombe donne un seul événement et l'outil le dit indisponible.

**Fait quand.** L'état des capacités se lit dans le journal et par l'outil.

---

## ORI-015 — Curiosité : pistes de recherche reliées aux objectifs

**Audit.** La curiosité produit des `ResearchLead` consommés par les objectifs : budget,
priorité, bénéfice attendu, sources, critères d'abandon ; la lettre devient une vue.

**Existant (largement fait).** D268 / ORION-10 (`0775873`) : recherche quotidienne pour un
objectif actif, immédiate quand un objectif passe `blocked` (une fois par blocage), trois par
jour au plus, chaque piste nomme son objectif ; `curiosity.run` porte déjà `goal_id`. `curiosity/module.py` : `aims()`, `stuck()`,
`tick()`, `hunt(now, trigger, focus)`, `_aims_block`, `brought()`, `_distill`, `Run` (trigger
`weekly|daily|blocked`). Objectifs : D267, `goals/module.py`.

**Manque.** Pas de bénéfice attendu ni de critère d'abandon journalisés ; budget global, pas par
piste. La piste n'est pas inscrite sur l'objectif (`goal.noted` existe dans `goals/module.py`,
la curiosité ne l'émet pas).

**Décidé.** D268 tient : la trouvaille passe par l'initiative, et c'est déjà l'arbitre qui
décide de se taire (la notification n'est pas forcée). En plus, chaque piste s'inscrit en note
de l'objectif (`goal.noted`), qu'elle soit dite ou non : l'objectif garde ce qu'on a cherché
pour lui.

**Plan.** Étendre `curiosity.run` (qui a déjà `goal_id`) avec `expected_benefit`, `stop_when`,
`budget` ; émettre `goal.noted` pour chaque piste ; la projection les lit. Test : un objectif bloqué produit une seule recherche, dont
l'événement nomme l'objectif et le budget.

---

## ORI-022 — Permissions visibles et historique des effets

**Audit.** Portée, expiration, révocation visibles ; historique des effets ; droit précis
(capacité, domaine, destinataire, montant, fréquence, horaire).

**Existant.** `guardian/autonomy.py` : `[guardian] autonomable`, `autonomy.granted` /
`autonomy.revoked`, `autonomy_grant`, révocation par mot, limites de paramètres (D167, D168).
`guardian/ledger.py` : approbations avec `expires_at`. `guardian/app.py` `_revoke` (appareils).
`mobile/static/app.js` : UI d'autonomie partielle (à vérifier). D26, D85, D90, D232, D247.

**Manque.** Pas d'expiration des autonomies ; pas de vue « portée + effets » dans l'app (à
vérifier dans `app.js`).

**Décidé.** **Aucune expiration par défaut** (D167/D168 tiennent : une autonomie accordée
tient). L'expiration est optionnelle, posée au moment d'accorder (« pour un mois »). Le mode
`libre` (F-02, P0) n'est pas touché ici. Les domaines consentis restent à ORION-12.

**Plan.** `expires_at` optionnel dans `autonomy.granted`, vérifié par la politique ;
endpoint `GET /v1/app/permissions` (autonomies, portée, expiration, derniers effets lus du
journal) ; la révocation réutilise le chemin existant. Test : une autonomie expirée redemande
l'accord.

---

## ORI-020 — Routeur coût/risque

**Audit.** Router par type de décision, risque et difficulté (rapide / fond / critique /
veille) ; mesurer reprises, corrections et réussite par modèle et par type.

**Existant.** `providers/router.py` `Router` (chaîne de repli, `router.fallback`,
`router.served_by_fallback`, `router.credits_unavailable`). Rôles dans `config.py` (~552-609) ;
`[models]` (D233) ; le travail de fond suit le modèle principal sur forfait (D147) ;
`providers/spending.py` ; `orion misses`, `orion week` (D152, D156 ; D273 y ajoute actions par
issue, appels coupés, travaux de fond, objectifs atteints et bruit, toujours lus du journal).
UNCERTAIN d'un appel coupé : D271. Jira ORION-17 (Backlog).

**Décidé.** **Mesurer avant de router.** D147 tient (le fond suit le modèle principal sur
forfait) : pas de routage par risque ou difficulté tant qu'aucune donnée ne montre qu'un rôle
échoue sur un modèle. Succès d'un tour = ni correction du propriétaire, ni reprise, ni Stop, ni
issue UNCERTAIN/FAILED dans le tour.

**Plan.** Chaque appel journalise déjà son rôle et son modèle (vérifier ; sinon l'ajouter à
l'événement d'appel existant, pas un nouvel événement) ; `orion week` (section de plus, à côté
de celles de D273) donne le taux de succès par rôle et par modèle. Test : une semaine avec une correction sur le rôle `tasks` fait baisser
son taux. Le routage par risque devient un ticket à part, le jour où les chiffres le
justifient.

---

## ORI-021 — Messages structurés multi-canaux

**Audit.** Séparer le résultat structuré (`kind`, `goal_id`, `headline`, `completed`…) de son
rendu par canal ; aucun canal ne perd d'information.

**Existant.** D126 (pièces structurées par balises), `channels/markup.py` ;
`initiative/engine.py` `NOTIFICATION_KINDS` / `notify_kind` (D225) ; chantier 5 de
`05-refonte.md` (« rien changé »).

**Décidé.** D126 tient : les balises restent le format du contenu (un seul texte, rendu par
canal). On n'ajoute pas d'objet message. Le manque réel est la perte au rendu : un canal qui
ignore une balise la perd.

**Plan.** L'événement d'émission de l'outbox porte `kind` (D225) et `goal_id` quand il existe ;
chaque canal (app, Telegram, iMessage, voix) a un repli texte pour **toute** balise de
`channels/markup.py`. Test : un message qui contient chaque balise, rendu par chaque canal, garde
toutes ses informations (le texte de chaque pièce est présent).

---

## ORI-023 — Baseline et pilote des routines

**Audit.** Baseline, hypothèse testable, petit changement, mesure d'adoption et d'effet sur
14 jours ; consentement explicite pour les domaines sensibles ; aucune causalité affirmée.

**Existant.** Rien de dédié. Voisins : objectifs à jalons et relecture du dimanche (D267), veille
à heures fixes (D150, D155), relevé de semaine (D156), ORION-18 (mesure hebdo, `recurrent`).

**Décidé.** Le consentement **est** la déclaration : une routine n'existe que si le
propriétaire la crée lui-même ; Orion ne propose jamais d'expérience de lui-même (les domaines
consentis restent à ORION-12). Pas de module : une routine est un objectif (`goals`) avec un
jalon de mesure, pour réutiliser états, relecture du dimanche et preuves. Dernier de la liste.

**Plan.** Événements `goal.baseline` et `goal.measured` (valeur, période) ; l'expérience =
un jalon daté de 14 jours ; le rapport lit le journal. Test : sur 14 jours simulés, le rapport donne l'écart à la baseline,
sans phrase causale.

---

## Non vérifié

Décision liée à ORION-4 (rappel des suites) ; UI des autonomies dans `mobile/static/app.js` ;
décision de référence du `Verifier`.
