# Orion

Agent personnel toujours actif et proactif — un second cerveau qui vit sur un serveur, te parle par
iMessage et au téléphone, et sait collaborer avec d'autres instances d'Orion, y compris celles
d'autres personnes.

## État

- **M0 — fondations** : journal append-only (SQLite WAL, horloge logique hybride), modèle
  d'événements avec provenance et confiance, projections reconstructibles, superviseur,
  configuration typée, logs structurés, CI.
- **M1 — premier cerveau** : client OpenRouter (DeepSeek v4.1 Flash par défaut), boucle d'agent
  (arrêts typés, budgets, détection de boucles, outils parallèles), contrat d'outil fail-closed,
  gardien v0, constitution (personnalité), contexte conçu pour le cache de prompt, canal terminal.
- **M2 — mémoire v0** : faits bi-temporels avec provenance (jamais écrasés, gardés en historique),
  registre des engagements (je dois / on me doit), recherche plein texte, outils mémoire,
  extraction en arrière-plan, oubli par source vérifié par rejeu du journal, profil lisible
  (`orion memory`), oubli (`orion forget <source>`).
- **M3 — iMessage et initiative v0** : canal iMessage via Inkbox (polling ou webhook signé,
  dédoublonnage, reprise après crash, rafales regroupées, indicateur de frappe, réponses en
  bulles), outbox idempotente, approbations par tapback ou par mot, rappels durables
  (`remind_me`), moteur d'initiative (3 points par jour sur les engagements, alerte avant une
  échéance, heures calmes, budget d'attention, « pourquoi maintenant »), démon `orion serve`.
- **Adaptation** : Orion apprend ton manuel. Il applique tout de suite ce que tu lui dis
  (`adapt`, `initiative_settings`). Une réflexion périodique tire des règles de tes messages et
  de tes tapbacks. Les points que tu ignores ne sont plus envoyés. Tout est visible
  (`orion memory`), corrigeable, et oublié avec la conversation d'où il vient.
- **M4 — fournisseurs étendus** : routeur avec modèles de secours, vérificateur sur un autre
  modèle pour tout message proactif, délégation des tâches longues au CLI `claude` officiel (en
  arrière-plan, résultat transmis à la fin). Le CLI comme modèle principal reste à faire (D22).
- **Boîte de réception unifiée** : tous les canaux passent par une seule file vers ton fil
  (D23). Un message envoyé pendant qu'Orion travaille rejoint le tour en cours, entre deux
  étapes : il peut changer de cap au lieu de finir un plan que tu as annulé.
- **M5 v0 — la voix, par Inkbox** :
  - au téléphone, la voix d'Inkbox est tenue à jour avec un brief tiré de la mémoire d'Orion ;
  - après chaque appel, la transcription revient dans ton fil et Orion fait ce qui a été convenu ;
  - Orion peut t'appeler, te réveiller par un appel, ou appeler un tiers avec ton accord ;
  - la ligne est réservée à ton numéro.

- **Modules** (D24) : Orion est une plaque sur laquelle on branche des modules. Le cœur garde la
  mémoire, le manuel, le gardien, la boîte de réception, l'initiative et les rappels. iMessage,
  la voix, la délégation et l'apprentissage en arrière-plan sont des modules, et d'autres peuvent
  s'installer comme paquets Python. `orion doctor` liste les modules et leur état.

- **M6 (en cours) — l'agenda** : module `calendar`. Orion lit tes agendas (Google, iCloud,
  Outlook, autant de comptes que tu veux) par leur adresse iCal secrète. Il te prévient avant
  chaque événement, met ta journée dans le point du matin et celle du lendemain dans le point du
  soir, et répond aux questions sur ton emploi du temps (`agenda`). Les invitations d'inconnus
  restent invisibles, et la description d'une invitation n'est jamais lue (D25). Lecture seule
  pour l'instant.

- **M6 (en cours) — le mail** : module `mail`. Orion lit tes boîtes en IMAP, en lecture seule
  stricte : rien n'est marqué comme lu. Il ne lit jamais un mail lui-même. Une quarantaine (un
  modèle à part, sans outils ni mémoire) en tire une fiche bornée : catégorie, importance,
  réponse attendue, échéance, montant, résumé d'une ligne. Un mail qui essaie de donner des
  ordres à un assistant est signalé comme suspect. Orion t'alerte pour un mail important, met
  les mails utiles dans tes points, et répond à tes questions (`mail`). Il ne peut pas encore
  en envoyer.
- **Contenu venu d'ailleurs** (D26) : quand une conversation contient un mail, une invitation
  ou un compte rendu d'appel, Orion ne peut plus agir sur le monde (envoyer, appeler), et il te
  demande ton accord avant de retenir quelque chose ou de programmer un rappel. Après deux
  heures sans échange, la conversation repart à neuf ; la mémoire garde l'essentiel.

Voir l'ordre de construction dans
[`docs/architecture/04-architecture.md`](docs/architecture/04-architecture.md).

## Démarrer

```bash
uv sync
cp orion.example.toml orion.toml
export OPENROUTER_API_KEY=...
uv run orion doctor
uv run orion chat
```

### iMessage

Il faut une clé Inkbox dans `INKBOX_API_KEY`. Dans `orion.toml`, section `[channels.imessage]`,
indique `enabled = true`, l'identifiant d'Orion (`identity`) et ton numéro (`owner_number`).
Ensuite :

```bash
uv run orion inkbox setup   # crée l'identité et affiche le message à envoyer depuis ton iPhone
uv run orion serve          # le démon : iMessage et initiative
```

Orion ne peut t'écrire qu'une fois que tu lui as écrit. Le mode `poll` (par défaut) n'a besoin
d'aucune URL publique. Pour le mode `webhook`, lance `orion inkbox setup --webhook-url
https://…` et place la clé de signature affichée dans `INKBOX_SIGNING_KEY`.

## Documentation

- [`docs/research/`](docs/research/) — études : Claude Code, paysage des agents, ce qui fait un Jarvis
- [`docs/architecture/04-architecture.md`](docs/architecture/04-architecture.md) — architecture
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — journal des décisions

### Agenda

Pour chaque agenda, récupère son adresse iCal secrète :
- **Google** : paramètres de l'agenda, « Adresse secrète au format iCal » ;
- **iCloud** : partage l'agenda en « Agenda public », puis copie le lien `webcal://` ;
- **Outlook** : Paramètres, Calendrier, Calendriers partagés, « Publier un calendrier », lien ICS.

Ces liens donnent accès à ton agenda : mets chacun dans une variable d'environnement, puis
déclare-les dans `[modules.calendar]` (voir `orion.example.toml`). Renseigne aussi
`owner_emails` : sans tes adresses, Orion ne sait pas distinguer tes événements des
invitations, et il les masque toutes. `uv run orion doctor` vérifie chaque compte.

### Mail

Pour chaque boîte, crée un mot de passe d'application (jamais ton vrai mot de passe) :
**Gmail** (`imap.gmail.com`, nécessite la validation en deux étapes) ou **iCloud**
(`imap.mail.me.com`). Outlook.com impose OAuth pour l'IMAP : pas encore pris en charge.
Mets chaque mot de passe dans une variable d'environnement et déclare la boîte dans
`[modules.mail]` (voir `orion.example.toml`). Le journal ne garde que les fiches, jamais le
contenu des mails ; `orion forget mail:perso` les efface.

## Écrire un module

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
(`weather = "mon_paquet:Weather"`), puis on l'active avec `[modules.weather] enabled = true`.

