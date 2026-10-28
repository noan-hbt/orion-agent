# Passation — 28/10/2026

Où en est Orion sur `main` (jusqu'à D177), pour qui le reprend. Les raisons de chaque choix sont
dans `DECISIONS.md` ; ce qui existe, module par module et commande par commande, dans
`REFERENCE.md` ; l'architecture dans `architecture/04-architecture.md` (son §17 s'arrête à
D84) ; la refonte de fiabilité dans `architecture/05-refonte.md`. La passation précédente
(20/10, D62 à D88) est dans l'historique git de ce fichier.

## Ce qui a changé depuis D84

- **L'app sur l'iPhone** (D125 à D143, D154, D170) : une PWA appairée par passkey, accords
  signés par Face ID, notifications, travail en cours avec Stop, tous les réglages. C'est le
  canal principal. Le gardien la sert en mode séparé (D134). Plus rien ne demande SSH (D154).
  Telegram (D88, D89) et iMessage par Inkbox restent, en option.
- **Les accords** : `agent.approvals = "equilibre"` par défaut (D85). Après un contenu venu
  d'ailleurs, agir sur le monde est demandé, pas refusé (D90, D130). Une autonomie accordée tient
  même après un mail ou une page (D168), et `autonomable = ["*"]` permet de tout confier (D167).
- **Ses réglages et ses capacités** : il change `orion.toml` lui-même avec ton oui (D92),
  installe skills, serveurs MCP et modules du catalogue (D95, D105), a un shell sur le serveur,
  root par sudo, chaque commande approuvée (D96, D164).
- **Son navigateur** : plus d'agent web séparé ni d'intermédiaires (D113, D114, D115). Un seul
  profil persistant sur le serveur, passé au propriétaire quand une page a besoin de lui (D116),
  un coffre d'identifiants façon KeePass (D111, D120, D142), un déguisement et une sortie par la
  machine du propriétaire (D145).
- **Les forfaits** : ChatGPT (D62) et Claude par le CLI `claude` (D100) ; un forfait reprend les
  rôles d'extraction, d'expert et de vision (D147).
- **La refonte** (D149 à D161, `05-refonte.md`) : situations, veille de jour, tiroir d'outils et
  un seul `later`, constitution courte, `orion misses` et `orion week`, contacts, flux, colis,
  Home Assistant.
- **La parole** : un message ne dit que ce qui est neuf, `tell` pour parler en cours de tour
  (D173), `<suite/>` pour couper en messages (D163), plus de barre de progression (D169).
- **La nuit** : une seule grande passe sur toute la mémoire, avec ses limites (D174) ; la passe
  du soir voit toutes les sections côte à côte (D176) et relève les ratés (D177).
- **Le serveur** : mise à jour automatique avec retour arrière (D94), remise à zéro et
  désinstallation (`deploy/reset.sh`, `deploy/uninstall.sh`).

## État

- 123 fichiers de tests, pyright strict à 0, ruff propre. 63 cas d'évaluation dans
  `evals/cases.py` ; dernière mesure dans `05-refonte.md` (49/57 après les chantiers 1 à 3).
- À faire d'après la refonte : les trajets et la banque (chantier 6). Le format par canal
  (chantier 5) n'a pas bougé.

## Écarts à connaître

- **Le budget du prompt** : D158 visait une constitution courte (24,4 k), mais un commit
  ultérieur a relevé les plafonds de `tests/test_prompt_budget.py` (40 k avec tous les modules,
  30 k par défaut) sans décision.
- **La mise à jour** : D94 dit toutes les 15 minutes ; `deploy/orion-update.timer` tourne toutes
  les 2 minutes.
- **D164** : le cœur est root par sudo. Le gardien n'est donc plus une frontière contre un cœur
  compromis (ce que supposait D28) ; il reste celle des accords et des secrets saisis dans
  l'app.
- **`orion notify`**, utilisé par la mise à jour, n'écrit que sur Telegram ; sans Telegram, le
  message ne va qu'au journal systemd.
- **Quota Inkbox** : 100 iMessages par 24 h par ligne, chaque bulle compte. Seule la ligne
  Inkbox est écrite.

## Faire tourner Orion

`orion.toml` et `data/` ne sont pas dans le dépôt (journal, données du propriétaire) : partir
de `orion setup` ou de `orion.example.toml`. Les secrets passent par l'environnement
(`SecretRef`) ou par le coffre du gardien ; les jetons des forfaits vivent dans `data/chatgpt/`
et `data/claude-code/`.

```
uv sync
uv run orion doctor
uv run orion guardian    # si [guardian] enabled : les secrets et les canaux
uv run orion serve       # le cœur, qui n'a que la clé du modèle
```

En production, voir `deploy/README.md` : deux services systemd, `orion-guardian` et
`orion-core`, sous deux utilisateurs distincts.
