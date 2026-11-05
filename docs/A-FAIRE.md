# À faire — feuille de route vers le Jarvis (05/11/2026)

La liste de travail issue de l'audit du 05/11 (`architecture/06-audit-jarvis.md`, le pourquoi,
les chiffres et les causes) et de la relecture des logs de prod du même jour. Elle est faite pour
être reprise par une nouvelle session : chaque point dit quoi faire, où, et comment savoir que
c'est fait. Coche, et note la décision (`Dxxx`) ou la PR en face.

Rappels : lire `CLAUDE.md` (règles du dépôt), travailler dans un worktree
(`git worktree add ../orion-<sujet> -b <branche> origin/main`). Accès prod en lecture :
`ssh vps 'sudo -n -u orion-core python3 -' < script.py` sur
`file:/srv/orion/data/journal.sqlite3?mode=ro` (table `events`, `hlc` = millisecondes epoch sur
15 chiffres), logs : `ssh vps 'sudo journalctl -u orion-core --since today -o cat'`.

## En cours

- [ ] **PR #178** (branche `fix-login-place`, worktree `../orion-login`) : un login ignore les
  formulaires d'un autre site, dit clairement « aucun formulaire ici, rien ne dit que le mot de
  passe soit faux », et retient où il est passé (`browser.signed_in`). À relire, merger, puis
  supprimer le worktree.
- [ ] **Modèles** : le propriétaire change lui-même le routage en prod (`/srv/orion/orion.toml`).
  Cible de l'audit : tout ce qui réfléchit (`strong_model`, `[models.watch]`, `reflection`,
  `curiosity`, `portrait`, `memory`/`extractor_model`, `thinking`) sur le meilleur modèle
  (`claude-code:opus` ou `chatgpt:gpt-6.1-sol`) ; `luna` seulement pour résumé et vision ;
  `verifier_model` renseigné. **Après son changement** : vérifier dans `model.usage` du journal
  que la répartition a bougé, et que les quotas (`plan.quota`) tiennent.

## P0 — petits correctifs à gros effet

1. [ ] **Prévenir quand le cerveau baisse.** Quand `router.served_by_fallback` dure (quota
   épuisé, comme le 05/11 à 19 h 06 : `subscription_sharing_usage_unavailable`), le dire au
   propriétaire une fois (au prochain point) et l'afficher dans l'app/`orion`.
   `src/orion/providers/router.py:64`.
2. [ ] **Engagements en retard.** `runtime.py:1030` (`due <= soon` sans borne basse) les
   montre dans chaque `<maintenant>` pour toujours (« à faire pour le 2026-11-03 » encore le
   05/11). Passé l'échéance : sortis de `<maintenant>`, une seule question (« fait ? reporté ?
   abandonné ? »), puis clos ou reprogrammés. Test : un engagement échu n'apparaît plus dans le
   contexte après la question.
3. [ ] **Déploiements groupés.** ~8 redémarrages/jour (110 initiatives `restart` en 14 j, 97
   tues) : chaque redémarrage ferme le navigateur et perd prises en main et onglets. Ne pas
   redémarrer pendant une conversation active (< 10 min) ni une prise en main ; regrouper (au
   plus une fois par heure, ou la nuit), sauf correctif marqué urgent. `deploy/orion-update.*`,
   D94.
4. [ ] **Anti-robot après prise en main.** Le 05/11, Kiabi (DataDome) bloque encore après que
   le propriétaire a passé le contrôle, alors que `Snapshot.wall()` promet « le site te laisse
   passer ensuite » (`computer/browser.py:335`). Comprendre pourquoi (cookie `datadome` non
   gardé ? empreinte différente entre son téléphone et le navigateur du serveur ? IP) ; corriger,
   ou ne plus promettre et proposer la sortie par sa machine (`--exit`).
5. [ ] **Relance « réponse vide » inutile.** Après `file` send (texte déjà parti avant), la
   boucle relance « ta réponse est vide… » (`loop.empty_reply`, `cognition/loop.py:449`) :
   un appel pour rien, deux fois le 05/11. Ne pas relancer quand le seul résultat non lu est un
   accusé d'envoi.
6. [ ] **Limites de `task` qui font perdre des étapes.** `budget_usd` > 3 refusé, `summary` >
   2000 caractères refusé deux fois de suite (05/11, 18 h 05 et 18 h 07). Plafonner/tronquer au
   lieu de refuser, ou dire la limite dans la description de l'outil.
7. [ ] **Onglet fermé entre deux messages.** « Envoie une capture » → « No page is open » puis
   réouverture (05/11, 13 h 35 et 15 h 13) ; une fois il a répondu « aucune page ouverte » au
   lieu de rouvrir. `screenshot` sans onglet devrait rouvrir la dernière page
   (`BrowsingState.last`) comme le fait déjà `read` (`_need_tab`).
8. [ ] **`fontTools` manquant** : avertissement à la lecture de certains PDF (05/11, 18 h 06).
   L'ajouter aux dépendances si la lecture en pâtit.

## P1 — les changements de structure (2 à 3 semaines)

9. [ ] **La preuve avant la parole.** Aucune réponse au propriétaire n'est vérifiée
   (`cognition/verifier.py` ne sert qu'aux initiatives, `initiative/engine.py:271,309` ;
   `finish.py` = regex sur 280 caractères). Cas réels : « oui, c'est cette rubrique vérifiée »
   (INPI, 05/11 9 h 14, chemin du mail jamais suivi), « connexion Nibelis rejetée par le
   coffre » (point de 16 h, faux), « pas moi » sur un accès Cyclades sans lire les logs (31/10).
   À faire : chaque résultat d'outil d'un tour reçoit un identifiant ; une réponse qui affirme un
   état (fait, envoyé, vérifié, aucun, rejeté, réussi) sans s'appuyer sur un résultat de ce tour
   repasse une fois au modèle ; le vérificateur juge les réponses factuelles. Évals : ces trois
   cas.
10. [ ] **Budget de contexte par étape.** La fenêtre n'est recalculée qu'au début du tour
    (`runtime.py:584-612`) ; chaque étape renvoie tout (médiane 70 k, p90 196 k, max 785 k
    jetons par appel, 605 M en 14 j). Au-delà d'un seuil, résumer sur place les résultats
    d'outils du tour courant. Ramener `max_steps` (100 en prod) vers 40. Ne pas casser
    `tests/test_runtime_cache.py`. Cible : p90 < 60 k.
11. [ ] **La veille qui travaille.** Aujourd'hui elle rend `SILENCE` / `MAINTENANT :` /
    `AU POINT :` (`watch/module.py`), 3 fois par jour au plus. Elle doit rendre une liste
    d'actions typées (faire seul, préparer, demander, dire), exécutées en tâches de fond ; le
    point ne présente que les résultats et les décisions à prendre. Déclenchée aussi par ses
    messages, plafond relevé.
12. [ ] **Briefing du matin**, préparé à l'heure de son réveil : sa journée, trajets et
    perturbations, ce qui coince dans les 48 h, ce qu'Orion a réglé dans la nuit, une décision à
    prendre au plus. **Revue du dimanche** : semaine passée, semaine qui vient, objectifs.
13. [ ] **Mémoire des procédures.** Objet `procedure` (site ou démarche, chemin, login du
    coffre, pièges, dernière réussite), écrit après une réussite vérifiée, injecté quand le site
    ou la démarche revient. Cas : Nibelis (`client.nibelis.com`), brouillons INPI (Entreprises →
    Créer, modifier ou cesser → SIREN), FranceConnect par L'Identité Numérique, Hyperplanning.
    La PR #178 en est le premier morceau.
14. [ ] **Récupération sur le contexte.** `<souvenirs>` = 6 faits cherchés sur le seul dernier
    message (`runtime.py:214,440,1109`). Chercher sur les derniers messages, les situations en
    cours et les noms cités. `<memoire>` tronquée en silence à 10 000 caractères
    (`config.py:312`) : dire ce qui manque, ou prioriser.
15. [ ] **Protéger ce qu'il a dit.** L'extracteur (SYSTEM) peut invalider un fait OWNER sans
    contrôle de code (`memory/service.py`, `_may_change`) ; 142 invalidations pour 275 faits en
    14 j. Un fait du propriétaire ne se remplace que par un autre de ses messages, ou devient une
    question.
16. [ ] **Ratés → évals.** 89 `adaptation.missed` en 14 j, aucun devenu cas. Chaque raté donne
    un brouillon de cas (`orion evals export`), relu chaque semaine. Faire tourner les évals sur
    le modèle de prod, pas seulement Haiku.

## P2 — le modèle de sa vie et les capacités (le mois)

17. [ ] **Objectifs et projets** : objet `goal` (domaine, but, échéance, jalons, situations et
    engagements liés, bilan hebdo). Le coaching et la curiosité s'y accrochent.
18. [ ] **Curiosité quotidienne** au service de ses objectifs (aujourd'hui une fois par semaine,
    samedi 6 h, 3 recherches, importance 0,25 : `curiosity/module.py:68-127`).
19. [ ] **Arbitre qui apprend** : chaque initiative reçoit un retour (répondue, ignorée,
    « merci », « pas maintenant ») ; l'importance par source s'ajuste sur 30 jours
    (`initiative/arbiter.py`).
20. [ ] **Domaines** (consentis, dans le gardien) : argent (GoCardless Bank Account Data en
    lecture, abonnements, fin de mois), santé (Raccourcis iPhone : sommeil, pas, sport), trajets
    (PRIM IDFM / SNCF), météo.
21. [ ] **Garde des achats** : `browser_click`/`type`/`fill` sont NONE
    (`computer/direct.py:1243`, voulu par D115) ; une page de paiement ou de confirmation
    détectée fait passer le clic final par le gardien, sauf autonomie pour ce site.
22. [ ] **Hooks la nuit** : rappels et rapports passent sans portail (`engine.py:621-627`) ; un
    hook à `then` parle donc la nuit et hors budget. Décider si c'est voulu.
23. [ ] **Fiabilité du web** : `web_read` échoue à 20 %, `browser` à 11 % (14 j). Classer les
    échecs (404, anti-robot, PDF, délai) et traiter les deux plus gros.
24. [ ] **Élaguer** une fois 9 et 10 en place : supprimer les relances `[Orion · …]` devenues
    inutiles (9 dans le code), fusionner les règles du manuel (195 apprises en 14 j ; cible
    < 60 actives).

## Indicateurs (mesurés au 05/11, à refaire chaque semaine)

| Indicateur | 05/11 | Cible |
|---|---|---|
| Messages d'anticipation par jour (pensée, curiosité, intention, objectifs) | 0,7 | 3 |
| Ratés relevés par semaine (`adaptation.missed`) | 42 | < 15 |
| Tours de plus de 60 s | 10 % | < 4 % |
| Jetons de prompt par appel, p90 | 196 k | < 60 k |
| Erreurs `web_read` / `browser` | 20 % / 11 % | < 10 % / < 6 % |
| Redémarrages par jour | ~8 | ≤ 2 |
| Appels de délibération sur un modèle fort | ~0 % | 100 % |
