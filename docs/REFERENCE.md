# Référence

Ce qui existe dans le code au 28/10/2026 (jusqu'à D177) : les modules, les commandes `orion`, la
configuration et les ports. Le pourquoi de chaque chose est dans [`DECISIONS.md`](DECISIONS.md).

## Les modules

Un module (D24) est une sous-classe de `orion.modules.base.Module` : il ajoute des consignes à la
constitution (`src/orion/resources/<nom>.md`), des outils, des services supervisés et des
comptes rendus pour l'initiative. La liste des modules livrés est `builtin_modules()` dans
`src/orion/modules/registry.py` ; d'autres se chargent par le point d'entrée `orion.modules` ou
s'installent depuis le catalogue (`orion modules install`, D105).

Un module tourne si sa section a `enabled = true`. Le chargement échoue (fail closed) pour un
module inconnu activé, ou pour un module activé dont un module requis ne l'est pas. La liste est
fixée au démarrage.

Une partie des outils est rangée dans un tiroir : l'outil `orion` les liste et les ouvre (D151).

### Allumés par défaut

| Module | Ce qu'il fait | Outils |
|---|---|---|
| `adaptation` | Apprend ton manuel : ce que tu veux, ce que tu ignores (`[adaptation]`) | `adapt`, `unlearn`, `initiative_settings`, `playbook` |
| `consolidation` | La passe de nuit sur toute la mémoire (D174) | service |
| `doubt` | Redemande ce qui a pu changer (D83) | `doubt_confirm`, `doubt_stale` |
| `feeds` | Flux RSS et Atom qu'il suit (D160) | `feed_add`, `feed_drop`, `feed_list` |
| `files` | Fabrique et envoie images, PDF, téléchargements (D60, D61) | `file` |
| `onboarding` | Faire connaissance, une question par jour au plus (D34) | `onboarding_stop` |
| `focus` | Concentration : seul l'urgent passe (D70) | `focus_start`, `focus_stop` |
| `expert` | Un modèle plus fort pour les questions dures (D46) | `consult_expert`, `think_harder` |
| `missions` | Des buts poursuivis seul pendant des jours (D47) | `mission`, `missions` |
| `tasks` | Des tâches de fond (D110) | `task` |
| `library` | Tes notes, tes documents, toute la conversation (D48) | `library_*`, `conversations_search` |
| `photos` | Photos et fichiers reçus, regardés une fois (D50, D63) | `attachment_look` |
| `papers` | Le coffre de tes papiers (D74, D131) ; requiert `files`, `photos` | `papers` |
| `thinking` | Repenser à ce qui est resté ouvert (D51, D117) | `think_later`, `follow_up*` |
| `watch` | La veille de jour sur ta vie (D150, D155) | service |
| `awaiting` | Attend une réponse et relance (D129) | `awaiting_*` |
| `chronicle` | La page de ta semaine, ses erreurs (D52, D69) | `chronicle`, `mistake` |
| `intentions` | Ce que tu veux devenir, et le droit de te contredire (D53, D66) | `intention_*`, `intentions` |
| `relations` | Anniversaires, avant de voir quelqu'un, garder le lien (D56) | `person_birthday`, `keep_in_touch`, `saw_someone`, `meeting_soon` |
| `contacts` | Le carnet d'adresses de ton téléphone (D159) | `contacts` |
| `departure` | La veille d'un départ ou d'un examen (D77) | `departure_*` |
| `advice` | Ses conseils et ce qu'ils ont donné (D65) | `advice_*` |
| `style` | Écrire comme toi (D68) | `owner_style` |
| `configure` | Changer ses réglages avec ton oui, demander un secret (D72, D92) | `configure_show`, `configure_change`, `secret_ask` |
| `vocal` | Vocaux : transcrits à l'arrivée, `say` pour répondre (D71) ; requiert `files` | `say` |
| `recap` | Le récapitulatif du soir | — |
| `skills` | Savoir-faire en texte et catalogue public (D95) | `skill` |
| `curiosity` | Une piste à lui par semaine (D76) ; ne tourne qu'avec `web` | `curiosity_*` |
| `booking` | Réserver un rendez-vous (D73) ; ne tourne qu'avec `computer` | `booking` |

### À activer

| Module | Section | Ce qu'il fait | Outils |
|---|---|---|---|
| `mobile` | `[modules.mobile]` | L'app iPhone : canal, accords Face ID, réglages (D125, D154) | canal |
| `telegram` | `[channels.telegram]` | Ton bot Telegram (D88, D89) | canal |
| `imessage` | `[channels.imessage]` | iMessage par une ligne Inkbox | canal |
| `voice` | `[voice]` | Le téléphone sur la ligne Inkbox (D40) ; requiert `imessage` | `call_me`, `call_someone` |
| `calendar` | `[modules.calendar]` | Tes agendas en iCal, écriture Google ou CalDAV (D25, D32) | `agenda`, `calendar_add` |
| `mail` | `[modules.mail]` | Tes boîtes en IMAP, lues en direct (D75, D171, D179) | `mail`, `mail_send`, `mail_tidy`, `mail_draft`, `mail_mark` |
| `web` | `[modules.web]` | Recherche et lecture de pages (D31) | `web_search`, `web_read` |
| `computer` | `[modules.computer]` | Son navigateur, persistant, passé au téléphone si besoin (D116) | `browser` |
| `console` | `[modules.console]` | La page de contrôle dans le navigateur (D39) | `console_link` |
| `hands` | `[modules.hands]` | Tes machines : fichiers partagés, notifications (D41) | `hands`, `hand_*` |
| `places` | `[modules.places]` | Rappels de lieu par les Raccourcis de l'iPhone (D57, D153) | `remind_at_place`, `places` |
| `secretary` | `[modules.secretary]` | Les proches qui peuvent écrire à Orion (D54) | `contact_note`, `contact_message` |
| `federation` | `[federation]` | Les autres Orion (D29) ; requiert `calendar` et le gardien séparé | `peers`, `meeting_*`, `peer_message` |
| `delegation` | `[delegation]` | Les longues tâches au CLI `claude` | `delegate`, `jobs_status` |
| `mcp` | `[modules.mcp]` | Serveurs MCP branchés (D95) | `mcp_<serveur>_<outil>` |
| `shell` | `[modules.shell]` | Le shell du serveur, chaque commande approuvée (D96, D164) | `shell` |

### Outils du cœur

Toujours là, hors modules : `remember`, `recall`, `forget_fact`, `commitment_*`,
`commitments_list`, `situation_*`, `situations_list`, `provenance`, `contain_source`, `person`,
`people`, `same_person`, `remind_me`, `reminder_*`, `reminders_list`, `planned`, `later` (D157),
`tell` (D173), `plan`, `autonomy`, `autonomy_revoke`, `spending`.

## Les paquets

| Paquet | Rôle |
|---|---|
| `core/` | Identifiants, horloge HLC, événements, journal append-only, projections, superviseur, sauvegarde |
| `guardian/` | Politique, accords, autonomie, secrets, coffre, outbox, registre, crypto de la fédération |
| `memory/` | Faits bi-temporels, engagements, personnes, provenance, situations, doute, passe de nuit, portrait |
| `initiative/` | Le moteur qui parle en premier : règles, rappels, points, arbitre, `later`, simulation |
| `cognition/` | La boucle d'agent (`loop.py`), aller au bout (`finish.py`), vérificateur |
| `context/` | Le compilateur de contexte (stable d'abord, pour le cache), fils, résumés, `<pendant_ce_temps>` |
| `providers/` | `openrouter`, `chatgpt`, `claude_code`, `router` (secours), `spending` (plafond) |
| `channels/` | Terminal, iMessage (Inkbox), Telegram, boîte de réception unifiée, balises de message |
| `tools/` | Le contrat d'outil (fail closed), outils groupés par domaine, `tell`, `plan` |
| `modules/` | Le contrat de module, le registre, l'installeur |
| `mobile/` | Le serveur de l'app, les notifications, les appareils, l'app web |
| `observability/` | Logs structurés (structlog) |
| `resources/` | Les consignes : `constitution.md`, `closing.md`, une par module |

Au sommet : `app.py` assemble Orion pour chaque point d'entrée, `serve.py` est le démon,
`setup.py` l'assistant d'installation, `config.py` la configuration typée, `cli.py` les commandes.

## Les commandes

`orion [--config orion.toml] <commande>`, depuis n'importe quel dossier après
`uv tool install --editable .`. Sans `--config` : `$ORION_CONFIG`, sinon `orion.toml` du dossier
courant, sinon `$ORION_HOME/orion.toml` (`~/.orion` par défaut, D180). Sur le serveur, `deploy/orion-admin` les lance sous
l'utilisateur du gardien (ou du cœur avec `--core`).

| Commande | Ce qu'elle fait |
|---|---|
| `setup [--server] [--secrets DIR]` | L'assistant : prénom, fuseau, gardien séparé, clé OpenRouter, Telegram |
| `doctor` | Vérifie la configuration, le stockage et chaque module |
| `chat` | Une conversation dans le terminal |
| `serve` | Le démon : canaux, initiative, modules |
| `guardian` | Le processus du gardien |
| `memory` | Ce qu'il sait de toi (écrit `profile.md`) |
| `forget SOURCE [--reason]` | Oublie tout ce qui vient d'une source |
| `contain SOURCE [--apply]` | Met en quarantaine une source et ce qu'elle a touché (D38) |
| `misses [--case N]` | Ses ratés ; `--case` en fait un cas d'évaluation |
| `week` | Sa semaine : tours interrompus, erreurs d'outils, corrections, coût |
| `simulate [--days 7]` | Ce qu'il enverrait, sans rien envoyer |
| `reminders list \| cancel ID` | Les rappels |
| `contacts import FICHIER.vcf` | Importer un carnet d'adresses |
| `library import DOSSIER` | Importer des notes |
| `logins add \| import \| remove \| list` | Le coffre des identifiants (D111) |
| `modules list \| install NOM \| remove NOM` | Les modules du catalogue |
| `app pair NOM \| devices \| revoke NOM` | Les téléphones de l'app |
| `hands pair NOM \| revoke NOM` | Les machines, côté serveur |
| `hand pair SERVEUR CODE [--root DIR] [--exit] \| exit on\|off \| run` | Sur ta machine |
| `chatgpt login \| status \| models \| logout` | Ton forfait ChatGPT |
| `google auth CLIENT_ID` | L'écriture dans Google Agenda |
| `inkbox setup [--webhook-url URL] \| calls` | La ligne iMessage et téléphone |
| `federation keygen \| peers \| invite \| accept \| revoke` | Les autres Orion |
| `console link` | Un lien de connexion à la console |
| `places token` | Le jeton des Raccourcis de l'iPhone |
| `notify TEXTE` | T'écrire sur Telegram (utilisé par la mise à jour) |
| `backup DEST [--keep 14] [--guardian]` | Une sauvegarde vérifiée |
| `check-backup CHEMIN` | Vérifier qu'une sauvegarde se restaure |
| `version` | La version |

## La configuration

`orion.toml`, décrit option par option dans [`orion.example.toml`](../orion.example.toml). Les
secrets n'y sont jamais : ils sont nommés (`SecretRef`) et lus dans l'environnement, ou dans le
coffre du gardien quand ils viennent de l'app.

| Section | Contenu |
|---|---|
| `[agent]` | Modèles (`model`, `strong_model`, `vision_model`…), accords (`approvals` : `equilibre` ou `prudent`, D85) |
| `[budget]` | Plafond de dépense par jour et par mois (D79) |
| `[providers.openrouter]`, `[providers.chatgpt]` | Les fournisseurs |
| `[channels.telegram]`, `[channels.imessage]`, `[voice]` | Les canaux |
| `[initiative]` | Points du jour, heures calmes, budget d'attention |
| `[adaptation]`, `[delegation]`, `[memory]` | Modules du cœur |
| `[guardian]` | Gardien séparé, actions qu'on peut lui confier (`autonomable`, `*` pour tout, D167) |
| `[federation]` | Les autres Orion : `public_url`, `listen_port` |
| `[modules.<nom>]` | Chaque module, avec `enabled` |
| `[logging]` | Niveau et format des logs |

## Les ports

Tout écoute sur `127.0.0.1` ; Caddy sert le HTTPS devant (`deploy/Caddyfile`).

| Chemin | Port | Processus |
|---|---|---|
| `/inkbox/webhook` | 8787 | cœur |
| `/.well-known/agent-card.json`, `/a2a` | 8788 | gardien (fédération) |
| `/console/*` | 8790 | cœur |
| `/voice/live` | 8791 | cœur |
| `/hands/*` | 8792 | cœur |
| `/places/*` | 8793 | cœur |
| `/secrets/*` | 8794 | gardien (formulaire des secrets) |
| `/app/*` | 8795 | gardien (l'app, l'appairage, les signatures, D134) |
| `/app/api/*` | 8796 | cœur |
