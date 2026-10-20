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

- **M6 — l'agenda** : module `calendar`. Orion lit tes agendas (Google, iCloud,
  Outlook, autant de comptes que tu veux) par leur adresse iCal secrète. Il te prévient avant
  chaque événement, met ta journée dans le point du matin et celle du lendemain dans le point du
  soir, et répond aux questions sur ton emploi du temps (`agenda`). Les invitations d'inconnus
  restent invisibles, et la description d'une invitation n'est jamais lue (D25). Il y écrit
  avec ton accord (D32).

- **M6 — le mail** : module `mail`. Orion lit tes boîtes en IMAP, en lecture seule
  stricte : rien n'est marqué comme lu. Il ne lit jamais un mail lui-même. Une quarantaine (un
  modèle à part, sans outils ni mémoire) en tire une fiche bornée : catégorie, importance,
  réponse attendue, échéance, montant, résumé d'une ligne. Un mail qui essaie de donner des
  ordres à un assistant est signalé comme suspect. Orion t'alerte pour un mail important, met
  les mails utiles dans tes points, et répond à tes questions (`mail`). Il en envoie avec ton
  accord (D32) et range ta boîte (D75).
- **Contenu venu d'ailleurs** (D26) : quand une conversation contient un mail, une invitation
  ou un compte rendu d'appel, Orion ne peut plus agir sur le monde (envoyer, appeler), et il te
  demande ton accord avant de retenir quelque chose ou de programmer un rappel. Après deux
  heures sans échange, la conversation repart à neuf ; la mémoire garde l'essentiel.

- **M6 — mémoire v1** : recherche par le sens et par les mots, fusionnées (« combien je paie
  pour l'appart ? » trouve le loyer). Les personnes de ta vie forment un graphe tiré de la
  mémoire : `person` dit tout ce qu'Orion sait de quelqu'un et à qui il est lié. Chaque nuit,
  la mémoire se range : doublons retirés, faits périmés remplacés, noms fusionnés
  (« Paul » = « Paul Martin »), contradictions posées en question dans le point du matin (D27).

- **Gardien séparé** (D28) : Orion peut tourner en deux processus. `orion guardian` détient
  les secrets (Inkbox, agendas, mail) et vérifie chaque effet sur le monde, même si le reste
  d'Orion était compromis :
  - les messages ne partent que vers ta conversation ;
  - la liste blanche de la ligne ne peut pas être levée ;
  - un appel à un tiers n'a lieu que si tu as dit oui, et le gardien le vérifie lui-même auprès
    d'Inkbox.

  `orion serve` ne garde que la clé du modèle, et refuse de démarrer s'il voit un autre secret.

- **M7 v0 — les autres Orion** (D29) : ton Orion se relie à celui d'une autre personne par une
  invitation à usage unique, échangée entre vous deux. Ensuite, « cale un déjeuner avec Paul la
  semaine prochaine » suffit. Orion propose tes créneaux libres, et rien d'autre de ton agenda.
  L'Orion de Paul choisit le premier qui lui va, et le soumet à Paul. Quand les deux ont dit oui,
  le rendez-vous est noté de chaque côté. Les messages sont signés, et seul le gardien les
  envoie ou les reçoit. Un Orion inconnu est refusé avant même d'être lu.

- **Sur un serveur** (D30) : `deploy/install.sh` installe Orion sur un VPS (Caddy en HTTPS,
  systemd, pare-feu). Le journal est sauvegardé chaque nuit (`orion backup`). Si le cœur tombe,
  le gardien te prévient lui-même.
- **Le web** (D31) : Orion cherche et lit des pages par un intermédiaire. Il n'ouvre que des
  adresses que tu as données ou qu'une recherche a trouvées.
- **Ses mains** (D32) : il ajoute des événements à ton agenda (Google ou CalDAV) et envoie des
  mails depuis ton compte, chaque fois avec ton accord, le texte exact sous les yeux. Les
  rendez-vous calés avec un autre Orion vont dans les deux agendas.
- **Son ordinateur** (D33) : un navigateur isolé, pour les tâches qui passent par des sites. En
  lecture seule, rien ne peut partir ; pour envoyer un formulaire, tu approuves la consigne.
- **Faire connaissance** (D34) : une question par jour au plus, les premières semaines.
- **Autonomie** (D35) : après plusieurs oui, Orion te propose une fois par mois de faire seul
  une action. Un mot suffit pour la lui retirer.
- **Proactivité v1** (D36) : il attend la fin d'une réunion pour ce qui peut attendre. Le
  dimanche soir, il te dit ce qu'il a failli te dire.
- **Engagements v1** (D37) : les promesses lues dans tes mails rejoignent tes engagements. Elles
  se clôturent avec une preuve (mail envoyé, événement ajouté).
- **Provenance** (D38) : « comment tu sais ça ? » a une réponse. Un mail piégé se met en
  quarantaine avec tout ce qu'il a touché.
- **Console** (D39) : une page pour voir et corriger ce qu'il sait et fait. Demande-lui le lien.
- **Voix v1** (D40) : au téléphone, c'est Orion lui-même qui répond, avec sa mémoire et ses
  outils pendant l'appel.
- **Mains sur tes appareils** (D41) : `orion hand` sur ton Mac ou ton PC. Il peut t'envoyer des
  notifications et trouver ou lire des fichiers dans les dossiers que tu partages.
- **Il conduit son navigateur** (D42) : il ouvre une page, la lit, clique, utilise la recherche
  d'un site, et décide de chaque étape lui-même. Rien ne part de ce navigateur.
- **Une boucle qui finit proprement** (D43) : à court d'étapes ou de budget, il te dit où il en
  est. Quand une longue conversation sort de sa fenêtre, il en garde un résumé.
- **Un modèle plus fort quand c'est dur** (D46) : il consulte Opus pour une question
  difficile, ou passe dessus pour la fin du tour.
- **Des missions** (D47) : « trouve-moi un appart » devient un but qu'il poursuit seul pendant
  des jours ; il ne t'écrit que s'il y a du neuf, et retient ce que tu écartes.
- **Une bibliothèque** (D48) : tes notes et documents gardés entiers, et toute votre
  conversation, datée et retrouvable.
- **Agir sur tes comptes, dans tes limites** (D49) : des mails ou des formulaires seul, mais
  seulement aux destinataires et sur les sites que tu as nommés ; tes sessions connectées
  restent chez le gardien, prêtées pour une tâche approuvée.
- **Des photos** (D50) : envoie-lui une affiche, une ordonnance, un ticket ou une capture
  d'écran ; il la lit, texte compris, et fait ce qu'elle appelle (agenda, rappel,
  bibliothèque, réponse). Ce qui est écrit sur une photo n'est jamais un ordre.
- **Il continue de penser** (D51) : la nuit, il repense à ce qui est resté ouvert et ne te
  revient le matin que s'il a trouvé du neuf. Il te demande comment s'est passé ton entretien,
  ton rendez-vous, ta visite.
- **La chronique** (D52) : chaque dimanche soir, la page de ta semaine, écrite pour toi et
  gardée. Au bout d'un an, un journal que tu n'as jamais eu à tenir. Une fois par mois, ce qu'il
  a vu revenir au fil des semaines (D58).
- **Tes intentions** (D53) : ce que tu veux devenir (courir plus, dépenser moins). Il les
  garde en tête, te le dit quand tu t'en éloignes, et fait le point chaque semaine.
- **Le secrétaire** (D54, à activer) : les proches que tu choisis peuvent écrire à Orion. Il
  leur répond sans rien savoir de ta vie (au plus, tes créneaux occupés), te transmet ce qui
  compte, et ne leur écrit en premier qu'avec ton accord.
- **Les gens de ta vie** (D56) : avant de voir quelqu'un, ce qui compte sur lui ; les
  anniversaires à temps ; et un mot quand ça fait trop longtemps que tu n'as pas vu quelqu'un
  qui compte.
- **Rappels de lieu** (D57, à activer) : « quand je passe à la boulangerie ». Ton iPhone ne
  lui dit que le nom du lieu où tu arrives, pour les lieux que tu as choisis.
- **Des fichiers** (D60) : demande-lui une image, une capture de la page qu'il regarde ou un
  PDF ; il te l'envoie dans la conversation.
- **N'importe quel fichier** (D61) : un fichier du web, de son serveur ou d'un dossier partagé
  sur ton ordinateur, tel quel, jusqu'à 10 Mo.
- **Ce qu'il pourrait faire de plus** (D64) : quand une demande aurait besoin d'un module
  éteint (ton LinkedIn, ton agenda), il le dit une fois, avec la commande pour l'activer.
- **Il assume ses conseils** (D65) : il te demande comment était le resto qu'il t'a conseillé,
  et apprend tes goûts plutôt que les notes.
- **Le droit de te contredire** (D66) : quand tu t'apprêtes à aller contre une de tes
  intentions, il le dit une fois, sans morale, et te laisse décider.
- **Le « à lire plus tard » qui revient** (D67) : un lien envoyé sans un mot est lu et gardé ;
  il revient le jour où la conversation y touche.
- **Écrire en ton nom** (D68) : un brouillon pour quelqu'un sonne comme toi (longueur,
  ponctuation, emojis, ta façon de parler à cette personne).
- **Le carnet d'erreurs** (D69) : quand il s'est trompé, il le reconnaît et le note ; la
  chronique du dimanche finit sur « où je me suis trompé cette semaine ».
- **La concentration** (D70) : « je bosse jusqu'à 18 h » ; seul l'urgent passe, le reste
  arrive en un seul message à la fin.
- **Tes fichiers** (D63) : une photo (HEIC compris), un PDF, un document Word ou Excel, un
  fichier texte : il le lit à l'arrivée et le garde. Un fichier audio, vidéo ou une archive,
  il le garde sans le lire.
- **Les vocaux** (D71) : envoie-lui un vocal, il le comprend ; demande-le-lui, ou dis-lui que
  tu conduis, il te répond en vocal.
- **Ses réglages par message** (D72) : « active le mail », « mets tes points à 9 h » ; tu
  approuves le changement exact, ou tu le laisses faire seul.
- **Tes rendez-vous** (D73) : « trouve-moi un dentiste après 18 h » ; il propose trois
  créneaux, réserve celui que tu choisis, et le met dans ton agenda avec ses rappels.
- **Tes papiers** (D74) : envoie-lui ta carte d'identité, tes fiches de paie, ton
  attestation d'assurance ; il les range, te prévient avant qu'ils expirent, les envoie sur
  ton accord et prépare ton dossier de location en un PDF.
- **Ta boîte mail, rangée** (D75) : désabonnement des newsletters et archivage par lots
  (un seul oui par lot), brouillons de réponse dans ta voix, pièces jointes.
- **Il est curieux** (D76) : une fois par semaine, il suit une piste à lui, là où ta vie
  croise ce que tu ne connais pas encore, et te rapporte une trouvaille seulement si elle vaut
  ton temps, avec son avis. Dis-lui ce qui t'intéresse, ou ce qui ne t'intéresse pas.
- **La veille d'un départ** (D77) : un train, un vol, un examen ; la veille au soir, un seul
  message avec le billet, tes papiers, la météo et l'heure à laquelle partir.
- **Ce qu'il a prévu** (D78) : « qu'est-ce que tu as prévu cette semaine ? » ; tout ce qu'il
  fera seul, jour par jour, et comment annuler chaque chose.
- **Un plafond de dépense** (D79) : ce qu'il fait seul s'arrête au plafond du jour ou du mois
  (`[budget]`), jamais ses réponses ; il te prévient. « Combien tu m'as coûté ? »
- **Une mémoire qui doute** (D83) : ce qui change souvent (ton logement, ton travail, tes
  projets) vieillit ; il te demande de temps en temps, en passant, si c'est toujours vrai.
- **Ton forfait ChatGPT** (D62) : `orion chatgpt login`, puis des modèles `chatgpt:<modèle>`
  dans `orion.toml`. Les appels comptent sur ton abonnement, pas sur des crédits.

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

### Deux processus (recommandé sur un serveur)

Mets `[guardian] enabled = true`. Ensuite, lance `orion guardian`, avec les secrets d'Inkbox,
des agendas et du mail, et `orion serve`, avec la seule clé OpenRouter. Le mieux est de les
lancer sous deux utilisateurs différents : voir [`deploy/`](deploy/).

### Relier ton Orion à celui de quelqu'un

Il faut le gardien séparé, le module `calendar` et `[federation] enabled = true`, avec une adresse
publique en HTTPS (`public_url`, un proxy inverse devant `listen_port`). Ensuite :

```bash
uv run orion federation keygen            # la clé va dans l'environnement du gardien
# sous l'utilisateur du gardien, une fois `orion guardian` lancé :
uv run orion federation invite --name Paul    # donne le code à Paul
uv run orion federation accept <code> --name Paul   # ou accepte le sien
uv run orion federation peers             # ton empreinte et tes Orion reliés
```

Vérifiez vos empreintes de vive voix.

### Vérifier

```bash
uv run orion simulate --days 7        # ce qu'Orion enverrait cette semaine, sans rien envoyer
uv run orion check-backup backups/core/<copie>   # une sauvegarde se restaure-t-elle ?
```

## Documentation

- [`docs/PASSATION.md`](docs/PASSATION.md) — où en est le travail, pour qui le reprend
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

