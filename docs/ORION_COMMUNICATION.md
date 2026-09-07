# Contrat de communication Orion

Cette version formalise les contrats communs du CLI et du gateway. Les
transports restent remplaçables : un message entrant conserve son identifiant
de requête et son `correlation_id`, et une sortie reporte la même corrélation.
L'acceptation d'une requête signifie qu'elle a été persistée dans le ledger et
placée dans la file bornée du processus ; elle ne signifie pas une livraison
exactly-once.

## Gateway

Le gateway est désactivé par défaut et se lie à `127.0.0.1`. Sa configuration
est validée dans `[gateway]` de `orion.toml` : `max_body_bytes` est borné à
256 KiB, `queue_size` à 1 000 et `request_timeout` est strictement positif.
Lorsqu'il est activé, Orion construit et enregistre automatiquement un
`HttpWebhookAdapter` nommé `gateway` dans `ChannelRouter`; les messages sont
écrits dans le ledger SQLite avant le `202 queued`. Toute authentification
utilise `auth_token_env` ou `hmac_secret_env`; les valeurs `token`, `secret`,
`password` et `api_key` en clair sont refusées. `allowlist` limite les sources
entrantes et `reply_allowlist` reste la politique des destinations de réponse.

En mode HMAC, chaque requête doit aussi porter `X-Orion-Timestamp` et
`X-Orion-Nonce`. La signature est le HMAC-SHA256 de
`timestamp + "\\n" + nonce + "\\n" + corps_brut`; le timestamp doit rester dans
`gateway.replay_window` (300 secondes par défaut) et un nonce ne peut être
réutilisé pendant cette fenêtre.

Une ancienne section `[channels.web]`, `[channels.api]` ou
`[channels.webhook]` doit déclarer `auth_token_env` ou `allowlist` avant d'être
activée. Cette règle évite une exposition implicite lors de la migration.

Le gateway répond `401` pour une authentification absente ou incorrecte,
`403` pour une source hors allowlist et `503` si sa dépendance de callback est
indisponible. Le contrat reste at-least-once : un crash après un effet externe
et avant l'ack peut entraîner un doublon.

## CLI

Le mode non-TTY lit une ligne à la fois depuis l'entrée fournie et écrit les
prompts et sorties sur la sortie fournie, ce qui fonctionne avec un pipe et
`StringIO` sur Windows comme Linux. EOF, `stop` et `/stop` sont des demandes
d'arrêt idempotentes. Le tracker expose les états `queued`, `running`,
`streaming`, `succeeded`, `failed` et `canceled`, ainsi que `request_id` et
`correlation_id`.

## Telegram

Telegram est fermé par défaut après son initialisation. Si aucune allowlist
n'est fournie et que `bootstrap_owner = true` (valeur par défaut), le premier
message entrant définit l'owner (`chat_id` et `user_id`) et est accepté ; cet
owner est persisté dans `owner_path` et les autres chats sont ensuite refusés.
Pour un démarrage strict sans bootstrap, définir `bootstrap_owner = false` et
fournir `allowed_chat_ids`, `allowed_user_ids` ou `allow_all_chats = true`. La
même règle s'applique aux sorties, sauf si `outbound_allowed_chat_ids` fournit
une allowlist de réponse dédiée. Les identifiants normalisés sont `update_id`,
`message_id` (forme `chat_id:message_id`) et `conversation_id` (le `chat_id`
texte).

Le curseur de long polling est sauvegardé atomiquement dans `offset_path`.
Sans fichier, un `CommunicationLedger` fourni peut conserver ce curseur dans
SQLite. Un update rejeté ou déjà dédupliqué fait progresser le curseur ; un
update qui rencontre une file pleine est rejoué. Les appels Bot API réessaient
de façon bornée les erreurs réseau, `429` et `5xx`, en respectant
`Retry-After`. `start()` et `stop()` sont idempotents et un adaptateur arrêté
peut être redémarré. Les réponses MarkdownV2 sont échappées ; en cas de
réponse Telegram `400`, l'adaptateur retente en HTML.

## Migration

`config_version = 2` est la version courante. Une configuration sans version
est lue comme version 2 si elle respecte les valeurs actuelles. Lorsque le
gateway est activé, `gateway.ledger_path` désigne le ledger SQLite de
communication, qui fournit des leases récupérables, replay et compteurs. Les
secrets restent référencés par leurs noms `*_env` dans TOML et leurs valeurs
restent dans `.env` ou l'environnement du processus.
