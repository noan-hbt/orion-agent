# Passation — 20/10/2026

Où en est Orion sur `main`, pour qui le reprend. Les raisons de chaque choix sont dans
`DECISIONS.md` ; l'architecture, et ce qui s'est écarté du plan, dans
`architecture/04-architecture.md` (§17).

## Ce qui a été fait depuis la dernière passation (D62 à D84)

- **D62 Forfait ChatGPT** : `orion chatgpt login`, puis des modèles `chatgpt:<modèle>`. La voie
  officielle « Sign in with ChatGPT », API Responses, jetons rotatifs dans `data/chatgpt/`.
- **D63 Tout fichier reçu** : image, document (PDF, Word, Excel…), texte, et le reste nommé
  sans être lu. Gardé avant d'être lu ; son contenu n'est jamais un ordre.
- **D64 Ce qu'il pourrait faire de plus** : chaque module déclare son `offer` ; Orion propose
  une fois l'activation quand une demande en aurait besoin.
- **D65 Ses conseils** (`advice`) : il demande comment c'était, et en fait un goût.
- **D66 Le droit de contredire** : `intention` (challenge), une fois par jour au plus.
- **D67 À lire plus tard** : un lien sans un mot est gardé et revient par le rappel de mémoire.
- **D68 Écrire en son nom** (`style`) : `owner_style`, projection du journal, sans modèle.
- **D69 Le carnet d'erreurs** : `mistake`, et la chronique du dimanche finit sur ses erreurs.
- **D70 Concentration** (`focus`) : seul l'urgent passe ; le reste en un message à la fin.
- **D71 Vocaux** (`vocal`) : transcription des siens, `say` pour répondre en vocal (ffmpeg).
- **D72 Configuration par message** (`configure`) : modifie `orion.toml` avec son oui,
  sections limitées, vérifié avant écriture, puis redémarre.
- **D73 Rendez-vous** (`booking`) : créneaux, réservation par `computer_task_send`, agenda et
  deux rappels ; c'est le code qui conclut, pas le modèle.
- **D74 Papiers** (`papers`) : coffre typé, alertes d'expiration, dossier de location en un PDF.
- **D75 Boîte mail rangée** : archivage et désabonnement par lots, brouillons dans sa voix,
  pièces jointes à `mail_send`.
- **D76 Curiosité** (`curiosity`, actif avec `web`) : une piste à lui par semaine, une
  trouvaille seulement si elle vaut le coup, son avis ; le verdict oriente la suite.
- **D77 La veille d'un départ** (`departure`) : voyage ou épreuve dans l'agenda (ou dit en
  passant), un message à 19 h la veille : billet, pièces d'identité, météo, heure de départ.

Le socle (D78 à D84) :
- **D78 Ce qui est prévu** : `Module.planned` dans chaque module, l'outil `planned`.
- **D79 Plafond de dépense** : `providers/spending.py` enveloppe le fournisseur dans
  `build_app` ; `foreground()` autour des tours du propriétaire ; `[budget]`.
- **D80 Versions des événements** : colonne `version` (migration sur place), `core/upcast.py`.
- **D81 Reconstruction prouvée** : `tests/conftest.py` rejoue chaque projection à la fin de
  chaque test ; `core/backup.verify`, `orion check-backup`.
- **D82 Simulation** : `orion simulate --days 7` (`initiative/simulate.py`).
- **D83 Mémoire qui doute** : `memory/doubt.py`.
- **D84 Budget du message système** : `MAX_SECTION_CHARS` dans `app.py`,
  `tests/test_prompt_budget.py`.
- La console montre ce qui est prévu (page « Prévu », D78) et ce qu'Orion a coûté (accueil,
  D79).

Les problèmes connus de la passation précédente (« demain » à 1 h du matin, longueurs et
ironie de DeepSeek, `computer_task` au lieu de `browser`, règles de contact d'exemple dans
Inkbox, envoi de fichiers par Inkbox) sont corrigés.

## État vérifié

- `uv run pytest -q` : 432 tests passent, chacun suivi du rejeu de ses projections. `uv run pyright` : 0 erreur. `ruff` : propre.
- 32 cas d'évaluation dans `evals/cases.py`. `train_pour_lyon` et
  `interesse_toi_aux_villes_sans_voiture` : 2/2 sur DeepSeek.
- En vrai : le forfait ChatGPT (outils, images, recherche web, JSON, PDF et Word lus), un vocal
  dit puis transcrit mot pour mot, `veille_de_partiel` 2/2 sur le forfait.
- Une chasse de curiosité réelle (Opus et le greffier du web) : piste « le projet Lumière »,
  trouvaille l'article de Horvitz sur l'initiative mixte (PDF, gardé par le résumé de la
  recherche).

## Pas encore vérifié en vrai

- **D73** : une réservation réelle (Doctolib, TheFork) par une tâche `computer`, avec une
  session prêtée.
- **D75** : archivage IMAP (UID MOVE ou copie et suppression), dépôt d'un brouillon, et
  désabonnement en un clic (RFC 8058) sur une vraie boîte.
- **D74** : alertes d'expiration (il faut un papier qui expire dans trois mois).
- **D72** en production : `orion.toml` appartient à root sur le serveur ; le rendre modifiable
  par l'utilisateur du noyau.
- Le secrétaire avec un vrai contact, les rappels de lieu de bout en bout, les constats
  mensuels (trois semaines de journal).
- D76 de bout en bout : la trouvaille arrivée par iMessage, puis le verdict.
- D77 avec un vrai billet dans la boîte mail.
- D79 : le plafond par défaut (3 $ par jour pour le fond, 40 $ par mois) n'a pas été comparé
  à la dépense réelle : regarder `spending` après quelques jours, et ajuster `[budget]`.
- D82 sur le vrai journal du serveur : `orion simulate` n'a tourné que sur des journaux de
  test.
- D80 : aucun type d'événement n'a encore eu besoin d'une conversion ; le mécanisme n'a
  servi que dans les tests.
- Pas de cas d'évaluation pour D75 : le harnais n'a pas de fausse boîte mail, et les outils du
  mail ont besoin d'un compte. D73 (`un_dentiste_apres_18h`) et D74 (`envoie_mon_attestation`)
  en ont un, 2/2 sur DeepSeek.

## Connu

- `test_approval_by_tapback` échouait de temps en temps : le délai d'approbation des tests
  (0,5 s) était trop court sur une machine chargée. Il est à 10 s, sauf pour le test d'une
  absence de réponse, qui a le sien.

## Faire tourner Orion

`orion.toml` et `data/` ne sont pas dans le dépôt (numéro du propriétaire, journal) : partir de
`orion.example.toml`. Les secrets passent par des variables d'environnement (`SecretRef`), sauf
les jetons ChatGPT, qui tournent et vivent dans `data/chatgpt/`.

```
uv sync
uv run orion doctor
uv run orion guardian            # garde les secrets (INKBOX_API_KEY…)
env -u INKBOX_API_KEY uv run orion serve   # le cœur, qui n'a que la clé du modèle
```

En production (OVH), voir `deploy/` : deux services systemd, `orion-guardian` et
`orion-core`, sous deux utilisateurs distincts.
