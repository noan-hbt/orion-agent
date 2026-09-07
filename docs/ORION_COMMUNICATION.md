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

## Migration

`config_version = 2` est la version courante. Une configuration sans version
est lue comme version 2 si elle respecte les valeurs actuelles. Lorsque le
gateway est activé, `gateway.ledger_path` désigne le ledger SQLite de
communication, qui fournit des leases récupérables, replay et compteurs. Les
secrets restent référencés par leurs noms `*_env` dans TOML et leurs valeurs
restent dans `.env` ou l'environnement du processus.
