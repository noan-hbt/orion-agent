# Ultrareview du 05/11 — chantier P1

Plan de reprise des travaux P1 de l'audit `docs/audit_orion_ultrareview_2026-11-05.pdf`
(registre §13, ORI-004 à ORI-013, plus les constats F-03, F-04, F-05, F-07), confronté au code
de `main` au 07/11/2026 (HEAD `8ad91fb`, jusqu'à D270). Les numéros de ligne sont indicatifs :
relire le code avant d'éditer. Le P2 est dans `08-ultrareview-p2.md`. Les P0 (F-01 root, F-02
mode `libre`) touchent D164 et D232/D247 et restent hors de ce chantier, à trancher par le
propriétaire.

Note : le tableau §13 du PDF a des colonnes décalées. Correspondance retenue : ORI-004 modèle
Goal, 005 Obligation, 006 Workflow durable, 007 idempotence/réconciliation, 008 contrats
d'achèvement, 009 migration Mission → goals, 010 jobs délégués reprenables, 011 Opportunity
Engine, 012 banc multi-jours, 013 métriques.

## Comment reprendre

**Avancement au 07/11.** Fusionnés : ORI-007 (#214), ORI-010 (#215), ORI-013 (#216), ORI-012 (#217), ORI-008 (#218), ORI-005 (#219). En revue : F-05 (ORION-43, D276). Fermés
sans code, en attente du propriétaire : ORI-004, ORI-006, ORI-009 (ORION-38). Reste : ORI-011
(ORION-6). Prochain Dxxx libre : D277.

- Un point = un ticket Jira `ORION-<n>` = une branche = une PR (règles dans `CLAUDE.md`).
  L'épopée est **ORION-1** (Backlog, label `audit-2026-11-05`). Une partie est déjà faite par
  ORION-9, 19, 20, 25, 32 (Terminé). Créer les tickets manquants en Backlog, liés à ORION-1.
- **Les choix sont tranchés** (07/11, délégués par le propriétaire) : voir « Décidé » dans
  chaque point. Chaque PR qui en applique un l'inscrit dans `docs/DECISIONS.md` (nouveau Dxxx,
  qui cite le Dxxx qu'il précise). Le ticket ORION-6 perd son `a-decider` (décision ORI-011).
- Ordre conseillé : ORI-007 → ORI-010 → ORI-004 → ORI-013 → ORI-012 → ORI-008 → F-05 →
  ORI-005 → ORI-011. ORI-009 se ferme sans code ; ORI-006 n'a rien de plus à faire.

| Point | État | Décision |
|---|---|---|
| ORI-007 idempotence (F-04) | fait : PR #214 (ORION-35, D271) | à coder |
| ORI-010 jobs délégués (F-03) | fait : PR #215 (ORION-36, D272) | à coder (check) |
| ORI-004 modèle Goal | fait (D267) ; rejeu couvert par `rebuild_check` (D81) — à fermer (ORION-38) | rien à coder |
| ORI-013 métriques | fait : PR #216 (ORION-37, D273) | à coder |
| ORI-012 banc multi-jours | fait : PR #217 (ORION-40) | à coder |
| ORI-006 workflow durable | fait (D257) — à fermer (ORION-38) | pas de graphe ni de compensation |
| ORI-008 contrats d'achèvement (F-07) | fait : PR #218 (ORION-41, D274) | étendre le contrat D267 |
| ORI-005 Obligation | fait : PR #219 (ORION-42, D275) | pas d'entité ; dédoublonner partout |
| ORI-009 migration Missions | absent — à fermer (ORION-38) | pas de migration, fermé |
| ORI-011 Opportunity Engine | partiel — décision commentée sur ORION-6 | le modèle propose un type, les règles décident |
| F-05 tâches en workflow | **en revue** (ORION-43, D276) | une passe = une étape ; missions inchangées |

---

## ORI-007 — Idempotence et réconciliation des actions (F-04)

**Audit.** Aucun effet doublé après une réponse perdue : `ACTION_PREPARED → DISPATCHED →
CONFIRMED`, clés d'idempotence stables, référence distante, état UNCERTAIN, réconciliation
avant tout retry. L'outbox est le modèle, pas le substitut.

**Existant.** `guardian/outbox.py` (`outbox.queued → delivered | failed`, id = clé, D19).
Workflows : `StepContext.key`, `_reconcile` avant retry, `check` par étape (D257).
`tools/outcome.py` `Outcome` (ATTEMPTED, PENDING, CONFIRMED, FAILED, UNCERTAIN) ;
`ToolResult.outcome/action_id/proof` (D258, ORION-20). `runtime.py` écrit `tool.started`
(~965, `call_id`, `deadline`) puis `tool.executed` (~982, `outcome`). Délai → UNCERTAIN pour un
outil à effet (D260, ORION-25) ; Stop (D261, ORION-32).

**Manque.**
- `tool.started` n'a **aucun lecteur** : un crash entre `started` et `executed` n'est jamais
  réconcilié.
- Aucune clé d'idempotence transmise aux connecteurs depuis la boucle de l'agent (seuls outbox
  et étapes de workflow en ont).

**Plan.**
- Au démarrage (sur le modèle de `WorkflowEngine.recover`) : pour tout `tool.started` d'un outil
  à effet externe sans `tool.executed`, journaliser `tool.executed` avec `outcome=UNCERTAIN` et
  le dire une fois au propriétaire. Jamais de rejeu.
- `ToolContext.idempotency_key`, dérivée de (thread, corrélation, outil, arguments normalisés),
  pas du `call_id` (le modèle peut en changer) ; l'honorer dans les connecteurs qui l'acceptent
  (mail, calendrier). Un connecteur qui ne peut pas : relire l'état distant avant retry.
- Tests : un `tool.started` orphelin devient UNCERTAIN sans relancer l'action ; deux tentatives
  identiques portent la même clé.

**Fait quand.** Après un crash en plein appel externe, l'état est UNCERTAIN, dit, et rien n'est
rejoué.

**État (07/11).** PR #214, ORION-35, D271. `tool.started` porte `may_act` et les arguments ;
projection `CutCalls` (`tools/interrupted.py`) ; `Orion.settle_cut_calls()` au démarrage du
démon seulement (`serve.start`, pas à chaque `Orion.start` : une commande CLI réglerait les
appels du démon) ; `CutCallReports` le dit une fois. `ToolContext.idempotency_key` honorée par
l'agenda (UID CalDAV + 412, id Google + 409). Mail SMTP : pas de clé possible.

---

## ORI-010 — Jobs délégués reprenables (F-03)

**Audit.** Persister spec, checkpoint, artefacts, curseur ; reprendre les étapes éligibles ;
distinguer processus interrompu et tâche non récupérable ; relire les journaux avant relance si
un effet externe a pu avoir lieu.

**Existant (largement fait).** D257 / ORION-19 (`2ad4f0f`) : `delegation/jobs.py` `JobService`
(`recover`, `start` avec `needs_session`, `_work`, `_done` idempotent) ; un job est un workflow
`delegation:<agent>` (étape `work`, `attempts=2`, effet EXTERNAL si shell, puis `finish`). Un job
coupé reprend, sauf s'il avait un shell (UNCERTAIN). Les options de session restent en mémoire,
**voulu par D257** (ne pas les persister). Test :
`tests/test_workflows.py::test_a_delegated_job_cut_by_a_restart_resumes_unless_it_had_a_shell`.

**Manque.** Pas de `check` sur l'étape `work` : un job shell coupé attend toujours le
propriétaire.

**Plan.** Écrire le `check` de `work` (lire `workdir` : fichiers produits, journal de l'agent
délégué) pour qu'un job shell coupé se règle seul quand la preuve existe. Test : « le check dit
fait → job SUCCEEDED sans relance ».

**Fait quand.** Un job shell coupé dont le travail est visible se termine sans relance ni
question.

**État (07/11).** PR #215, ORION-36, D272. Le CLI écrit son JSON final dans
`<workdir>/.orion-result.json` ; `DelegatedAgent.left` le relit, c'est le `check` de `work`.
Les fichiers seuls ne prouvent rien (un travail à moitié fait en laisse aussi).

---

## ORI-004 — Modèle Goal

**Existant (fait).** D267 / ORION-9 (`28ac23c`) : `goals/module.py` — `GoalState`, `Goal`,
`Milestone`, `Note`, projection `GoalBook` (rebuildable), service `Goals` (`set`, `change`,
`move`, `milestone`, `reach` avec preuve, `link`, `note`…), outil `goal`, événements `goal.*`.
Tests : `tests/test_goals.py`.

**Manque.** Le rejeu du journal n'est pas testé ; rappel quand la date de réévaluation passe
(signalé par D267).

**Plan.** Un test : journal → `GoalBook` reconstruit → mêmes statuts. Le rappel de réévaluation
va dans un ticket séparé (amélioration, pas P1).

**Fait quand.** Le test de rejeu passe ; ORI-004 se ferme.

**État (07/11).** Rien à coder : `tests/conftest.py::rebuild_check` (D81) reconstruit chaque
projection après chaque test, `GoalBook` compris (4 tests de `test_goals.py`). Fermeture :
ORION-38. Rappel de réévaluation : ORION-39 (Backlog).

---

## ORI-013 — Métriques de valeur, fiabilité et bruit

**Audit.** Tableau de bord qualité hebdomadaire.

**Existant.** `adaptation/week.py` (D156, `orion week`, `GET /api/week`) : tours arrêtés,
outils en échec, corrections, coût. `adaptation/misses.py` (D152). `console/dashboard.py`
(D101). `observability/` n'a que `logging.py`.

**Manque.** Issue des actions, fiabilité des workflows, délais, bruit, objectifs atteints.

**Plan.** Étendre `week.py` (aucun événement nouveau, tout est journalisé) : compter les
`outcome` de `tool.executed`, `workflow.step_uncertain`, `tool.timed_out`, messages ignorés
(`Ledger.ignored`, SILENCE), `goal.reached` ; afficher dans le tableau de bord. Test : une
semaine qui contient une action UNCERTAIN la montre dans le relevé.

**Fait quand.** `orion week` répond « qu'est-ce qui a vraiment été fait, raté, incertain, et
combien de bruit ».

**État (07/11).** PR #216, ORION-37, D273 : issues des actions et incertaines une à une,
appels coupés au délai, workflows par état, étapes incertaines, objectifs atteints, bruit
(parlé de lui-même, sans réaction sous 3 h, choses tues). L'affichage dans l'app reste à faire.

---

## ORI-012 — Banc multi-jours et entrées hostiles

**Existant.** `evals/` (D45 : `harness.py`, `cases.py`, `--offline`), `initiative/simulate.py`
(`orion simulate --days 7`, D82), ORION-8 (ratés → cas), `tests/test_workflows.py`.

**Manque.** Scénarios multi-jours sans réseau ; jeu d'entrées hostiles ; `simulate` non testé.

**Plan.** `tests/test_bench_multiday.py` : `time_machine`, faux fournisseur, journal de test.
Trois scénarios : objectif réévalué au jour 30 ; workflow en attente de quota qui reprend le
lendemain ; page web qui demande une action, refusée ou demandée par le gardien. Ne pas
multiplier les tests (règle de `CLAUDE.md`).

**Fait quand.** `uv run pytest -q tests/test_bench_multiday.py` passe sans réseau.

**État (07/11).** PR #217, ORION-40. Les trois scénarios passent sur `main` sans code de
production ; redémarrage du processus entre les jours. Pas de Dxxx.

---

## ORI-006 — Workflow durable

**Existant (fait).** D257 / ORION-19 : `workflows/engine.py` — `State` (dont WAITING,
UNCERTAIN), `Step` (`run`, `effect`, `check`, `attempts`, `timeout`), `RunProjection`,
`WorkflowEngine` (`start`, `recover`, `resume_all`, `_reconcile`, `cancel`), backoff,
`QUOTA_WAIT`. `workflows/module.py` (outil `workflows_status`). Tests dans
`tests/test_workflows.py`, dont la coupure d'un effet externe vérifiée, jamais rejouée.

**Manque.** Un seul utilisateur (la délégation). Étapes linéaires (D257), sans graphe ni
compensation.

**Décidé.** Ni graphe ni `Compensation` : D257 (étapes linéaires) tient. Aucun cas réel ne les
demande ; ils s'ajouteront le jour où un workflow en a besoin, dans sa propre PR.

**Plan.** Rien en propre. La généralité se prouve par F-05 (les tâches deviennent le deuxième
utilisateur du moteur).

**État (07/11).** Fermeture : ORION-38.

---

## ORI-008 — Contrats d'achèvement vérifiables (F-07)

**Audit.** « Terminé » = résultat observé : `expected_outcome`, `acceptance_checks`,
`evidence_requirements`, `verify_after`, `human_signoff`.

**Existant.** `cognition/finish.py` (regex, messages du propriétaire seulement) ;
`cognition/verifier.py`, `cognition/grounding.py` (D252, D253, D270) ; `Goals.reach` exige une
preuve `CONFIRMED` (D267, D258). Mais `Tasks.finish`, `Missions.close` et `Job._done` ne
vérifient rien.

**Décidé.** Pas de nouveau modèle de contrat : on **étend celui de D267** (`done_when` + preuve
`CONFIRMED`) aux tâches et aux jobs délégués. Les missions, récurrentes, n'ont pas de « fini »
et restent hors du contrat. `verify_after` et `human_signoff` ne sont pas repris : une preuve
absente laisse l'objet ouvert et le dit, c'est déjà la vérification humaine.

**Plan.** Champ optionnel `done_when` sur `Task` et `Job` (texte rédigé à la création, comme
pour un objectif). Quand il existe, `Tasks.finish` et `JobService._done` exigent une action
`CONFIRMED` du journal liée à l'objet (même règle que `Goals.reach`, réutiliser `_confirmed`) ;
sinon l'objet finit `unproven` (nouvel état, dit au propriétaire), jamais « fait ». Test : une
tâche avec `done_when` et sans preuve ne se dit pas finie.

**État (07/11).** PR #218, ORION-41, D274. Tâche : `done_when` au start, `seen` au done,
sinon `unproven`. Job : rien de l'agent n'est journalisé CONFIRMED, donc un job réussi avec
`done_when` arrive « non prouvé » et Orion vérifie lui-même. Règle unique :
`tools/outcome.confirmed_proof` (ex-`Goals._confirmed`).

---

## ORI-005 — Obligation et fusion des engagements

**Existant.** `memory/store.py` `Commitment` (debtor, creditor, what, due, status),
`memory/service.py` `add_commitment`/`close_commitment`, outils `commitment_*` ; anti-doublon
`memory/tools.py` `same_commitment` à l'ajout (D37) ; échéances par l'initiative (D250) ;
objectifs liés par id (`goals/module.py` `LINK_KINDS`).

**Manque.** Pas de fusion entre sources (mail, veille, objectif, tâche).

**Décidé.** D267 tient : **pas d'entité Obligation**. L'engagement (`Commitment`) est déjà
l'obligation ; un objectif ou une tâche s'y lie par id. Le vrai manque de l'audit est le doublon
entre sources : on le règle là où naît l'engagement.

**Plan.** Toute création d'engagement, quelle que soit la source (outil, extraction de mail,
veille), passe par un seul point dans `memory/service.py` qui applique `same_commitment` ; un
doublon devient une `commitment.seen` (nouvelle source rattachée à l'engagement existant), pas
un second engagement. Lister d'abord tous les chemins qui créent un engagement. Test : un même
engagement venu d'un mail puis de la veille n'existe et n'est rappelé qu'une fois.

**État (07/11).** PR #219, ORION-42, D275. `MemoryService.record_commitment` : seul point de
naissance (outil, consolidation, rendez-vous fédérés) ; doublon ouvert → `commitment.seen`.
`same_commitment` compare l'heure quand les deux en ont une.

---

## ORI-009 — Migration des Missions vers les objectifs

**Existant.** `missions/service.py` `Missions` (source de vérité, `every_hours`, D47) ; les
objectifs ne font que lier (`LINK_KINDS` contient `mission`).

**Décidé.** **Pas de migration.** Mission = ce qu'Orion fait en continu, objectif = le résultat
voulu (D267 : liens, pas de copie). Migrer doublerait la vérité. ORI-009 se ferme sans code ;
le ticket éventuel le dit en citant D267.

**État (07/11).** Fermeture : ORION-38.

---

## ORI-011 — Opportunity Engine événementiel

**Existant.** `watch/module.py` (`Watch`, `conclusion` : SILENCE / MAINTENANT / digest,
`Stir`, D155) ; `initiative/engine.py` (`Candidate`, `Ledger`, `_admit`), `arbiter.py`
(D20, D144, D146, D269) ; curiosité liée aux objectifs (D268).

**Manque.** Type d'opportunité (FAIRE / PRÉPARER / DEMANDER / INFORMER / SILENCE), objet
opportunité (sources, valeur, prérequis, échéance, fusion), journal de détection et de choix.

**Décidé.** Le modèle **propose** un type (FAIRE / PRÉPARER / DEMANDER / INFORMER / SILENCE)
avec sa raison ; **les règles et l'arbitre décident** s'il interrompt et quand (D20 et D146
tiennent). FAIRE passe toujours par le gardien comme toute action ; PRÉPARER ne produit qu'un
brouillon, jamais un effet. Pas de nouveau package : l'opportunité est le `Candidate` existant,
enrichi. Cette réponse lève le `a-decider` d'ORION-6, qui porte le travail (commenter le
ticket avec cette décision, puis le passer en `Selected for Development`).

**État (07/11).** Décision commentée sur ORION-6, `a-decider` retiré ; le passage en
`Selected for Development` reste à faire par le propriétaire.

**Plan.** `Candidate` gagne `kind` et `reason` ; la veille (`Watch.conclusion`) rend un type
par `Stir` ; journaliser `opportunity.detected` (clé stable = source + objet, donc une seule par
chose) et `opportunity.decided` (issue de l'arbitre, y compris le silence). Test : deux
détections identiques ne font qu'une opportunité, journalisée même sans message.

---

## F-05 — Tâches et missions en workflow

**Existant.** `tasks/service.py` (`Task`, `Tasks`, passes bornées, D110 : une tâche est Orion
dans un fil à lui), `tasks/module.py`, `missions/service.py` ; aucun ne passe par
`WorkflowEngine`.

**Décidé.** **Une passe de tâche = une étape de workflow** ; la tâche garde son fil à elle
(D110 tient pour la conversation, seule l'exécution change). On gagne gratuitement reprise
après crash, UNCERTAIN si une passe a pu agir, attente de quota. Les missions restent inchangées
(passes récurrentes `every_hours`) : une passe perdue est simplement refaite à l'échéance
suivante. À faire après ORI-008.

**Plan.** `tasks/module.py` démarre un `Workflow` `task:<id>` dont l'étape `pass` appelle la
passe actuelle (`effect` = EXTERNAL si la passe a des outils à effet) ; `Tasks` lit l'état du
run. Supprimer le code de relance maison qui fait doublon. Test : une tâche coupée en pleine
passe reprend au redémarrage sans refaire les passes finies.

---

## Non vérifié

Lecture exacte de `finish_check` dans `runtime.py` (~713) ; existence d'un ticket hors du label
`audit-2026-11-05` qui recouvrirait un point.
