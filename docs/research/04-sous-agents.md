# Étape 4 — Un aide qu'Orion attend : les sous-agents synchrones

> Objectif : décider si Orion doit pouvoir confier un travail de lecture à un aide **pendant**
> un tour, et attendre sa réponse, au lieu de tout lire dans la conversation du propriétaire.
> Comparer avec les agents qui le font, et dire ce que ça changerait (et ne changerait pas).
>
> Date de l'étude : 25 octobre 2026. Décision : D123 (proposée).

---

## 0. Méthode et fiabilité des sources

| Source | Fiabilité | Remarque |
|---|---|---|
| Docs officielles de Claude Code (`code.claude.com/docs/en/sub-agents`, Agent SDK) | ★★★ | Comportement actuel, pas l'implémentation |
| Docs officielles de Codex CLI (`learn.chatgpt.com/docs/agent-configuration/subagents`) | ★★★ | Idem |
| Docs officielles de Cline (`docs.cline.bot/features/subagents`) | ★★★ | Peu de détails chiffrés |
| opencode (`opencode.ai/docs/agents`), OpenHands SDK (`openhands-tools/.../delegate/`) | ★☆☆ | Le code source n'a pas pu être lu : **non vérifié** |
| Code d'Orion (`tasks/`, `guardian/policy.py`, `runtime.py`, D110, D114) | ★★★ | Lu pour cette étude |

Le code de Claude Code n'a pas été utilisé : seulement sa documentation publique.

## 1. Le problème

Tout ce qu'Orion lit pendant un tour entre dans la conversation du propriétaire : pages, résultats
de recherche, mails. Le fil grossit, chaque étape relit tout (D110 l'avait constaté : 45 000
jetons relus à chaque étape d'une grosse recherche), et l'essentiel se noie dans le reste.

Les tâches de fond (D110) règlent ça pour le travail long, mais en différé : Orion répond « je
lance une recherche », et le résultat arrive plus tard par le moteur d'initiative. Pour une
question qui demande de lire cinq pages (« compare ces trois offres d'abonnement »), il n'existe
rien entre lire tout soi-même dans le fil et répondre plus tard.

## 2. Ce que font les autres

| | Claude Code | Codex CLI | Cline | opencode (non vérifié) |
|---|---|---|---|---|
| Appel | outil `Agent` : consigne, type, modèle | demandé par l'utilisateur ou les instructions | `spawn_agent` | outil `task` |
| Contexte de l'enfant | neuf (sauf « fork », qui copie la conversation) | non documenté | fenêtre à part | session enfant |
| Ce qui revient | son dernier message seulement | une réponse regroupée | sa réponse finale | non documenté |
| Outils | liste par type ; Explore et Plan en lecture seule | hérite du bac à sable et des accords | ceux du mode Act | permissions par agent |
| Imbrication | 3 niveaux au plus | non documentée | oui | non vérifié |
| Limites | `maxTurns`, budget, 20 en parallèle | plafond de fils simultanés | aucune documentée | `steps`, puis résumé |
| Attente | premier plan (bloque) ou arrière-plan (prévient plus tard) | le parent attend tout | le parent attend tout | arrière-plan expérimental |
| Accords | hérite des règles du parent | hérite, accords remontés avec le nom de l'enfant | approuver l'enfant approuve tout | non documenté |
| Contenu piégé | rien de dédié : le rapport est scanné, les actions qu'il déclenche restent soumises aux permissions | non documenté | non documenté | non documenté |

Ce qui est commun : une consigne part, l'enfant travaille dans un contexte neuf, **seul son
dernier message revient**, ses lectures restent chez lui. Les variations : le choix entre
attendre et laisser tourner (Claude Code fait les deux), et l'étendue des outils de l'enfant.

Aucun de ces agents ne prétend qu'un enfant protège des pages piégées. Le rapport d'un enfant
qui a lu une page vaut cette page.

## 3. Ce qu'Orion a déjà

- **Les tâches de fond (D110)** sont déjà des sous-agents : Orion lui-même, dans son propre fil
  (`task:<id>`), avec une consigne complète, un budget (dollars, minutes, 20 passages), et un
  plafond de tâches simultanées. Elles ne parlent qu'à Orion. Leur rapport arrive par une
  alerte, avec la confiance la plus basse de ce qu'elles ont lu.
- **Le gardien les cantonne à la recherche** (`WORKER_TOOLS` : web, navigateur, téléchargement,
  PDF). Une tâche ne lance pas d'autre tâche (`inside(ctx)`).
- **Un verrou par fil** (`Orion._locks`) : un tour sur `task:<id>` peut tourner pendant que le
  tour du propriétaire attend, sans blocage.
- **D114** : le propriétaire ne veut aucun autre esprit entre ses informations et Orion. Les
  tâches sont compatibles avec D114 parce que l'aide est Orion lui-même (même modèle, même
  constitution, même mémoire), pas un lecteur à part.

Il manque donc seulement la possibilité d'**attendre** l'aide dans le tour.

## 4. Ce qu'un aide attendu apporterait, et ce qu'il n'apporterait pas

Il apporterait :
- **un fil plus léger** : les pages lues restent dans le fil de l'aide ; Orion ne reçoit que son
  bilan ;
- **une réponse dans le même tour** : plus besoin de « je lance une recherche, je te redis » pour
  un travail de deux minutes ;
- **du parallèle** : deux ou trois aides sur trois offres, au lieu de les lire l'une après
  l'autre.

Il n'apporterait pas :
- **de confiance en plus.** Le bilan d'un aide qui a lu le web a la confiance du web. Le fil
  d'Orion baisse autant que s'il avait lu la page lui-même, et les accords demandés restent les
  mêmes (D26, D90). Un gain de confiance demanderait une sortie bornée (des champs typés, pas de
  texte libre), c'est-à-dire la quarantaine que D114 a retirée. Ce n'est pas proposé.
- **forcément des économies.** L'aide relit son propre fil à chaque étape, en plus du tour
  d'Orion. Le gain vient du fil d'Orion qui ne grossit plus ; sur une petite lecture, l'aide
  coûte plus qu'il ne rapporte.
- **de vitesse sur une seule lecture.** Démarrer un aide ajoute au moins un appel au modèle.

## 5. Les options

**A. Attendre une tâche** (recommandée). `task` start prend une attente, en secondes. Si l'aide
finit dans ce délai, son bilan revient comme résultat de l'outil, dans le tour. Sinon, il
continue en fond, comme aujourd'hui, et Orion le dit au propriétaire. C'est le premier plan et
l'arrière-plan de Claude Code, avec une seule mécanique.

**B. Un nouvel outil `explore`**, plus léger : un tour unique sur un fil jetable, sans budget en
dollars ni passages, en lecture seule, qui rend sa réponse. Plus simple à expliquer au modèle,
mais une deuxième façon de faire la même chose, avec ses propres limites à tenir.

**C. Ne rien faire.** Les tâches de fond, l'élagage des vieux résultats et la pagination des
pages couvrent déjà une partie du besoin.

## 6. La proposition détaillée (option A)

- **Interface** : `task` start gagne `wait` (0 à 180 s, 0 par défaut : le comportement
  d'aujourd'hui). La description dit quand l'utiliser : plusieurs pages ou sources à lire pour
  répondre maintenant.
- **Pendant l'attente** : l'outil fait tourner les passages de l'aide lui-même jusqu'à `done`,
  une question (`wait`), une limite ou la fin du délai. Le propriétaire voit que ça avance par
  la progression de D121 (« je cherche sur le web… », PR #29 en cours).
- **Ce qui revient** : le bilan de `done` (ou le dernier rapport), borné à 4 000 caractères, avec
  la confiance du fil de l'aide (`window_trust`). Le résultat d'outil la porte, donc le fil
  d'Orion baisse exactement comme aujourd'hui.
- **À la fin du délai** : l'aide continue en fond ; le résultat dit « elle continue, elle
  t'écrira ». Rien n'est perdu.
- **Plusieurs aides à la fois** : `task` start devient sûr en parallèle quand `wait` est donné, sous
  le plafond de tâches simultanées.
- **Ce qui ne change pas** : le cantonnement du gardien (`WORKER_TOOLS`), pas d'imbrication, le
  budget de l'aide, le journal (chaque passage est journalisé dans `task:<id>`), le cache de
  prompt d'Orion (le bilan est un résultat d'outil comme un autre).
- **Tests** : un aide qui finit à temps rend son bilan ; un aide trop long continue en fond ; la
  confiance du bilan est celle de ses lectures ; deux aides attendus tournent en parallèle ; une
  question de l'aide pendant l'attente rend la main à Orion.

## 7. Questions ouvertes (au propriétaire)

1. **D114** : un aide attendu reste Orion lui-même, mais Orion ne voit plus la page, seulement
   ce que son aide en dit. Est-ce acceptable ?
2. **Les outils de l'aide** : rester à la recherche web (D110), ou ouvrir la lecture de ses mails,
   de sa bibliothèque et de ses conversations, en lecture seule ?
3. **Le délai** : 180 s au plus sur Telegram, est-ce trop long pour un tour ?
