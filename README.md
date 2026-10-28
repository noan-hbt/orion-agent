# Orion

Agent personnel toujours actif et proactif : un second cerveau qui vit sur ton serveur, te parle
depuis une app sur ton iPhone (ou Telegram, iMessage, le téléphone), se souvient de ta vie, agit
pour toi avec ton accord, et sait s'entendre avec les Orion d'autres personnes.

Orion ne se contente pas de répondre. Il garde tes engagements, te prévient avant ce qui
compte, repense la nuit à ce qui est resté ouvert, poursuit des missions pendant des jours, et
ne te parle en premier que quand ça vaut ton attention.

## Ce qu'il fait

**Se souvenir.** Des faits datés et sourcés, jamais écrasés ; les engagements (je dois, on me
doit) ; les situations qui durent (D149) ; les gens de ta vie et leurs liens. Recherche par le
sens et par les mots. Chaque nuit, une passe relit toute la mémoire : doublons, contradictions,
faits périmés (D174). Ce qui change souvent vieillit, et il te redemande si c'est toujours vrai
(D83). « Comment tu sais ça ? » a toujours une réponse (D38). Oublier une source efface tout ce
qui en vient.

**Parler en premier, avec mesure.** Points du matin et du soir, alerte avant une échéance,
veille trois fois par jour sur ta vie (D150), relances quand quelqu'un ne répond pas (D129),
rappels ponctuels, récurrents ou de lieu, heures calmes, concentration (« je bosse jusqu'à
18 h »), au plus 12 messages par jour de sa propre initiative (D104). Chaque prise de parole est
une décision journalisée, avec son « pourquoi maintenant ».

**Ta vie quotidienne.** Agendas (Google, iCloud, Outlook ; lecture et écriture), mail (IMAP,
tri, brouillons dans ta voix, envoi, rangement par lots), papiers (rangés, expiration, dossier
de location en un PDF), photos et fichiers lus à l'arrivée (ce qui y est écrit n'est jamais un
ordre), vocaux dans les deux sens, contacts, flux RSS, colis et abonnements, Home Assistant par
MCP (D161), rendez-vous réservés pour toi (D73), la veille d'un départ (D77).

**Penser et chercher.** Le web ; son propre navigateur, qu'il conduit seul et qu'il te passe
quand une page a besoin de toi (D116) ; un modèle plus fort pour les questions dures (D46) ; des
missions de plusieurs jours (D47) ; des tâches de fond (D110) ; une curiosité à lui, une fois
par semaine (D76) ; une bibliothèque de tes notes et de toute votre conversation (D48).

**Te connaître.** Il apprend ton manuel (ce que tu veux, ce que tu ignores), écrit comme toi
quand il rédige pour quelqu'un (D68), garde tes intentions et ose te contredire une fois (D53,
D66), suit ses conseils (D65), relève ses erreurs et ses ratés (D69, D177), et tient la
chronique de ta semaine (D52).

**Agir, dans tes limites.** Tout ce qui sort passe par le gardien. Agir sur le monde (mail,
message à un tiers, formulaire, commande shell) se fait avec ton oui, le texte exact sous les
yeux, ou seul dans les limites d'une autonomie que tu lui as accordée et que tu retires d'un mot
(D35, D167). Un plafond de dépense borne ce qu'il fait seul (D79).

**Les autres Orion.** Relie ton Orion à celui de quelqu'un par une invitation ; « cale un
déjeuner avec Paul » suffit, et seuls tes créneaux libres sont partagés (D29, D99, D102).

## Comment c'est fait

- **Un journal append-only** (SQLite WAL, horloge logique hybride) est la seule vérité. Le reste
  (mémoire, personnes, rappels, missions…) est une projection qui se reconstruit à partir de
  lui. Chaque contenu porte sa provenance et son niveau de confiance.
- **Deux processus** (D28) : `orion guardian` détient les secrets et les canaux, et vérifie
  chaque effet sur le monde ; `orion serve`, le cœur, pense. Sur un serveur, ils tournent sous
  deux utilisateurs. En local, le gardien séparé est optionnel.
- **Une plaque et des modules** (D24) : le cœur garde le journal, la mémoire, le manuel, le
  gardien, la boîte de réception unifiée, l'initiative et les rappels. Tout le reste est un
  module qu'on active dans `orion.toml`. Voir [`docs/REFERENCE.md`](docs/REFERENCE.md).
- **Une boucle d'agent** (`cognition/loop.py`) qui va au bout (D118), avec des budgets, des
  outils en parallèle, et un contexte fait pour le cache de prompt : chaque requête prolonge la
  précédente octet pour octet.
- **Les modèles** : OpenRouter par défaut (`deepseek/deepseek-v4.1-flash` au quotidien,
  `anthropic/claude-opus-5.5` pour les questions dures), ou tes forfaits : ChatGPT
  (`chatgpt:<modèle>`, D62) et Claude (`claude-code:<modèle>`, par le CLI `claude`, D100). Un
  forfait reprend les rôles d'extraction, d'expert et de vision (D147).

## Démarrer

### Sur un serveur (recommandé)

Sur un VPS Debian 12 ou Ubuntu 24.04 :

```bash
git clone https://github.com/noan-hbt/orion-agent.git && cd orion-agent
sudo deploy/install.sh orion.mondomaine.fr     # ou sans argument : <ip>.sslip.io
```

Le script installe tout, pose quelques questions (prénom, fuseau, clé OpenRouter, Telegram en
option) et affiche le code pour appairer l'app sur ton iPhone. Ensuite, plus rien ne se fait en
SSH : réglages, secrets, modules, forfaits, machines et autres Orion se gèrent dans l'app,
signés par Face ID (D154). Les mises à jour se font seules. Détails dans
[`deploy/README.md`](deploy/README.md).

### Sur ta machine

```bash
uv tool install --editable .   # une fois, depuis le dépôt : `orion` marche ensuite partout
orion setup     # prénom, fuseau, clé OpenRouter, bot Telegram en option
orion chat      # lui parler dans ce terminal, sans aucun canal
orion serve     # le démon (avec `orion guardian` à côté si le gardien est séparé)
```

`orion setup` écrit `~/.orion/orion.toml`, et les secrets dans `core.env` et `guardian.env` à
côté (jamais dans `orion.toml`). Un `orion.toml` dans le dossier courant passe avant ;
`ORION_HOME` ou `ORION_CONFIG` changent l'emplacement (D180). Après un `git pull`, rien à
refaire : l'installation suit le dépôt. Toutes les options sont décrites dans [`orion.example.toml`](orion.example.toml).
Ensuite, dis-lui « active le mail » ou « mets tes points à 9 h » : il change ses réglages
lui-même, avec ton accord sur le changement exact (D92).

### Les canaux

- **L'app** (module `mobile`, D125) : une PWA sur l'écran d'accueil de l'iPhone, appairée par
  passkey. La conversation, les accords signés par Face ID, les notifications, le travail en
  cours et tous les réglages. C'est le canal principal.
- **Telegram** (`[channels.telegram]`) : ton propre bot, gratuit, proposé par `orion setup`.
- **iMessage et téléphone** (`[channels.imessage]`, `[voice]`) : par une ligne Inkbox, payante
  (`orion inkbox setup`). Au téléphone, c'est Orion lui-même qui répond (D40).
- **Le terminal** : `orion chat`.

### Connecter tes comptes

- **Agenda** : l'adresse iCal secrète de chaque agenda (Google : « Adresse secrète au format
  iCal » ; iCloud : agenda public, lien `webcal://` ; Outlook : « Publier un calendrier »), dans
  `[modules.calendar]`. Renseigne `owner_emails`, sinon tes événements passent pour des
  invitations et sont masqués.
- **Mail** : un mot de passe d'application par boîte (Gmail `imap.gmail.com`, iCloud
  `imap.mail.me.com`), dans `[modules.mail]`. Outlook.com (OAuth) n'est pas pris en charge.
- **Forfait ChatGPT** : Réglages de l'app, ou `orion chatgpt login`.
- **Tes machines** : `orion hand pair <serveur> <code>` sur ton Mac ou ton PC, pour qu'il lise
  les dossiers que tu partages et t'envoie des notifications (D41). Avec `orion hand exit on`,
  son navigateur sort par ta connexion (D145).

### Vérifier

```bash
orion doctor                 # configuration, stockage, état de chaque module
orion simulate --days 7      # ce qu'il enverrait cette semaine, sans rien envoyer
orion week                   # tours interrompus, erreurs d'outils, corrections, coût
orion misses                 # ses ratés : tes 👎 et tes corrections
```

## Développer

```bash
uv sync
uv run pytest -q tests/test_x.py                   # seulement les fichiers touchés
uv run ruff format . && uv run ruff check .
uv run pyright --pythonplatform Linux              # strict, 0 erreur
uv run python -m evals                             # évaluations sur le vrai modèle
```

Les règles du dépôt (journal append-only, fail closed, `SecretRef`, contrat du cache de prompt,
tests sur le comportement) sont dans [`CLAUDE.md`](CLAUDE.md).

### Écrire un module

Un module est une sous-classe de `orion.modules.base.Module`. Chaque crochet est facultatif :

```python
from orion.modules.base import Daemon, Module, ModuleSettings, Plate


class WeatherSettings(ModuleSettings):  # section [modules.weather] d'orion.toml
    city: str = "Paris"


class Weather(Module):
    name = "weather"
    settings_model = WeatherSettings
    requires = ()  # autres modules nécessaires

    def instructions_text(self) -> str:  # ajouté à la constitution
        return "# La météo\n\nTu connais la météo avec `weather`."

    async def assemble(self, plate: Plate) -> list[Tool]:
        return [...]  # outils donnés au modèle

    async def serve(self, daemon: Daemon) -> None:
        daemon.add_service("weather.watch", self.watch)  # service supervisé
        daemon.reports.append(self)  # comptes rendus pour l'initiative
```

On le publie comme paquet avec un point d'entrée dans le groupe `orion.modules`
(`weather = "mon_paquet:Weather"`), puis on l'active avec `[modules.weather] enabled = true`. Ou
on l'ajoute au [catalogue](catalog/README.md) : Orion l'installe alors sur ton oui (D105).

## Documentation

- [`docs/REFERENCE.md`](docs/REFERENCE.md) — les modules, les commandes, la configuration
- [`deploy/README.md`](deploy/README.md) — installer et tenir Orion sur un serveur
- [`docs/PASSATION.md`](docs/PASSATION.md) — où en est le travail, pour qui le reprend
- [`docs/architecture/04-architecture.md`](docs/architecture/04-architecture.md) — architecture
- [`docs/architecture/05-refonte.md`](docs/architecture/05-refonte.md) — la refonte de
  fiabilité (D149 à D161)
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — journal des décisions : la raison de chaque choix
- [`docs/research/`](docs/research/) — études : Claude Code, paysage des agents, ce qui fait un
  Jarvis, l'application
- [`catalog/`](catalog/README.md) — skills, serveurs MCP et modules que tout Orion peut installer
