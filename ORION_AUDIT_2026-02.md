# Orion — Audit technique consolidé

**Périmètre :** 42 modules Python de premier niveau, 37 776 lignes (39 519 hors tests), 90 fichiers de tests (697 fonctions), 6 paquets d'outils, 15 documents.
**Méthode :** 12 agents d'analyse en parallèle (un par sous-système + audit transverse + outillage statique), puis contre-vérification directe dans le code par l'agent coordinateur. `build/` exclu partout (copie obsolète).
**Statut :** lecture seule. Aucun fichier source modifié.

Chaque constat porte sa référence `fichier:ligne`. La mention **[vérifié]** signale une confirmation obtenue en lisant directement le code, et non en faisant confiance au rapport d'un sous-agent.

---

## 1. Ce qu'est le projet

Orion est un agent IA personnel **piloté par événements**, en Python, mono-processus multi-threads (pas d'asyncio dans le chemin principal).

Boucle : un message arrive par un *channel* → `EventHandler.publish` (dédup + boîte durable SQLite) → file de priorité → un worker → `AgentRuntime.receive_event` → `_wake` → boucle d'agent LLM (`_run_agent_loop`) → appels d'outils (politique → approbation → ActionLedger → exécution) → réponse via le *ChannelRouter* → journal.

Sous-systèmes :

| Domaine | Modules |
|---|---|
| Cœur | `runtime.py` (6 163 l.), `event_handler.py`, `durable_events.py` |
| Délégation | `subagents.py`, `teams.py`, `tasks.py`, `workflows.py`, `handoff_context.py` |
| Contexte/mémoire | `context_assembler.py`, `prompt_context.py`, `context_os.py`, `context_registry.py`, `memory_store.py` |
| Durabilité | `action_ledger.py`, `communication_ledger.py`, `outbox.py`, `approvals.py` |
| E/S | `channels.py`, `channel_adapters.py` (5 adaptateurs), `scheduler.py` |
| Outils | `tool_manager.py`, `tool_policy.py`, `orion_toolbox.py`, `github_tools.py`, `tool_packages/*` |
| UI | `orion_run.py`, `cli_cockpit*.py`, `cli_ui.py`, `cli_v2.py` |
| LLM | `openrouter_client.py`, `reflection_engine.py` |
| Config | `orion_config.py` (1 633 l.), `orion_install.py`, `orion_vps_install.py` |

**Ce qui est solide (à ne pas casser) :**
- **Aucun import circulaire** au niveau module (vérifié par SCC de Tarjan sur les 42 modules).
- **Pyflakes et `ruff --select F,E9` totalement propres** sur 137 fichiers : zéro nom indéfini, zéro import inutilisé, zéro erreur de syntaxe.
- **Défense SSRF du tool web réellement robuste** : résolution unique, `ipaddress.is_global` sur chaque adresse, connexion sur l'IP épinglée avec vérification SNI, revalidation à chaque redirection.
- Intégrité des paquets d'outils fail-closed (SHA de blob GitHub, SHA-256 opérateur, garde zip-slip).
- Discipline `*_env` respectée pour les secrets ; allowlist e-mail fail-closed par défaut.
- 78 `pytest.raises` tous typés, zéro `MagicMock`, 918 usages de `tmp_path`.

---

## 2. Défauts confirmés, par gravité

### CRITIQUE

**C1. Aucune journalisation en production, combinée à ~113 exceptions avalées.**
`import logging` / `getLogger` : **zéro occurrence** dans tout le dépôt. `observability.JsonLogger` n'est instancié que dans les tests (`tests/test_p1p2_modules.py:10`) et est un no-op sauf si `ORION_LOG_JSON=1` (`observability.py:78`). Par ailleurs ~113 gestionnaires dont le corps est `pass` (dont 24 dans `channel_adapters.py`, 11 dans `cli_cockpit.py`, 8 dans `runtime.py`).
*Conséquence :* un démon durable qui perd des messages, voit mourir ses threads ou échouer ses écritures ne produit **aucune trace**. La plupart des pannes listées ci-dessous sont silencieuses pour cette raison. **[vérifié]**

**C2. Telegram : les valeurs par défaut livrées et documentées jettent silencieusement tous les messages entrants.**
`orion.toml:126-133` documente `bootstrap_owner = true` sans allowlist ni `bootstrap_pairing_secret_env`. Or l'adaptateur exige un secret pour appairer (`channel_adapters.py:1906-1920`) et **refuse explicitement** de déduire l'ownership du trafic entrant (`channel_adapters.py:1939-1943`, durcissement délibéré). Sans allowlist, sans owner et sans secret : `channel_adapters.py:1944-1946` exécute `_persist_offset(update_id+1); continue` — message jeté **et** curseur avancé, donc Telegram ne le renverra jamais. `validate()` accepte cet état (`orion_config.py:676-685`).
Pire : `orion_vps_install.py:270-280` appelle `_config_text` avec **9 arguments positionnels** alors que `telegram_pairing_secret_env` est le **10ᵉ** paramètre (`orion_install.py:235`) — il vaut donc `None` et le secret de pairing n'est jamais écrit. Le déploiement VPS Telegram documenté produit un bot totalement muet et non appairable. Contredit `README.md:143-144`, `orion_vps_install.py:7-9`, `DEPLOYMENT_VPS.md:3-5`, `docs/ORION_COMMUNICATION.md:48-53`. **[vérifié, indépendamment par 3 agents]**

### ÉLEVÉ

**H1. Mort silencieuse et définitive des threads de travail (3 sous-systèmes, même anti-pattern).**
`event_handler.py:631-651`, `subagents.py:1675-1684`, `teams.py:643-687` : les boucles `_worker`/`_poll` entourent le corps d'un `try/finally` **sans `except`**. Toute exception hors bande (`sqlite3.OperationalError`, `OSError` disque plein, verrou de fichier Windows) tue le thread. `_running` reste `True`, donc `start()` sort immédiatement (`event_handler.py:539-540`) et plus rien ne redémarre. Avec `events.workers = 1` par défaut, c'est **tout le pipeline d'événements** qui s'arrête. **[vérifié]**

**H2. Les outils privilégiés s'exécutent sans aucune approbation par défaut.**
`ApprovalConfig.enabled = False` (`orion_config.py:229`), aucune section `[approvals]` dans `orion.toml` → `set_approvals_enabled(False)` (`orion_config.py:1221`) → `approval_required` toujours faux → `_approval_gate` laisse passer. Dès que l'opérateur active le tool `terminal` (`orion.toml:239,245`), toute commande shell s'exécute sans invite. `subagents.py:2051` fait `approved = not approvals_enabled`. **[vérifié]**
S'ajoutent : les approbations n'expirent jamais et ne sont pas à usage unique (`runtime.py:4297-4342`, `approvals.py:60-81`) — une commande approuvée une fois se rejoue indéfiniment dans un contexte de tâche durable ou planifiée ; et l'algorithme d'identité est déterministe.

**H3. Les tentatives facturées auprès du fournisseur sont invisibles dans le registre de coûts.**
`openrouter_client.py:685-713` refait un `POST chat/completions` sur timeout/5xx/429 sans clé d'idempotence, et `_compatibility_fallback_payload` renvoie **le corps entier** après un 400 (`:962-979`). Or `_finish_usage` (`:493-560`) **écrase** `record.prompt_tokens/completion_tokens/cost_usd` avec l'usage du dernier essai. Chaque génération produite puis retentée est payée mais jamais comptée. **[vérifié]**
Aggravant : `estimated_cost_usd` agrège `cost_source == "catalog_estimate"` (`:197`) mais rien n'écrit jamais cette valeur (`:546` n'écrit que `"openrouter"`, défaut `"unavailable"` `:105`) — la colonne « estimé » du CLI (`cli_ui.py:955`) est structurellement à zéro. **[vérifié]**

**H4. `Retry-After` non borné bloque un thread sans annulation possible.**
`openrouter_client.py:612` : `return max(0.0, float(retry_after))` — aucun plafond — puis `time.sleep(...)` (`:713`), bloquant et non interruptible. Un `Retry-After: 3600` gèle un worker une heure ; le runtime ne teste l'interruption qu'entre deux appels fournisseur. **Incohérence notable :** l'adaptateur Telegram fait correctement les deux choses (`channel_adapters.py:1842-1843`) — plafond via `retry_max_delay` **et** attente interruptible via `_stop_requested.wait(delay)` — alors que le client LLM ne fait ni l'une ni l'autre. **[vérifié]**

**H5. `max_attempts` ne borne pas le rejeu après crash.**
`event_handler.py:826` applique bien le plafond, mais `attempts` n'est incrémenté que dans la branche d'exception de `_dispatch` (`:821`). Un crash en cours de run fait rejouer le reçu par `recover_stale` (`durable_events.py:603-632`) avec `attempts` toujours à 0 : la boucle n'est pas bornée par le plafond configuré. **[vérifié — la formulation « max_attempts jamais appliqué » d'un rapport était trop forte ; voici la version exacte]**

**H6. Verrous/backpressure : `_durable_lock` tenu pendant un `queue.put` bloquant.**
`event_handler.py:523-532` prend `_durable_lock` puis appelle `queue.put(event, timeout=…)` ; la seule voie de drainage `_queue_get` (`:653-658`) et `_process_event` (`:690`) exigent le même verrou. Un producteur lent gèle **tous** les workers. Et `queue_size = 0` par défaut (`orion_config.py:63`, `orion.toml:40`) rend la file non bornée : tout le chemin `EventQueueFullError` est mort. **[vérifié]**

**H7. Orphelins du graphe d'imports : 5 modules (~1 050 lignes) jamais importés en production.**
`outbox.py`, `workflows.py`, `identity_resolver.py`, `cli_cockpit_events.py`, `cli_v2.py` — importés uniquement par leurs propres tests. `outbox.py` est une **seconde implémentation** de la durabilité sortante, la vraie étant `CommunicationLedger` ; `identity_resolver.py` définit un `Principal` de forme différente de `channels.Principal` ; `cli_v2.py` est pourtant déclaré livré (`pyproject.toml:40`) et documenté (`docs/CLI_V2.md`). **[vérifié]**

**H8. Fuites mémoire non bornées (plusieurs, indépendantes).**
- `_durable_receipts_by_event_id` : écrit en `event_handler.py:437,678` et `runtime.py:771,5546`, lu en `:691` — **jamais retiré** côté `event_handler` (`runtime.py:3559` le fait pourtant, ce qui prouve l'asymétrie involontaire). **[vérifié]**
- `_dead_letters` (`:244,843-846`) et `_callback_errors` (`:254`) : croissance infinie.
- `actions` : aucun `DELETE` dans `action_ledger.py`, et `reserve()` balaie toutes les lignes de l'opération sur 24 h avec un `SequenceMatcher` par candidate (`:299-327`) → table infinie **et** coût O(n²) par appel d'outil.
- `communication_events` : jamais élagué ; seuls les nonces le sont (`communication_ledger.py:702-705`).
- Transcript du cockpit : réaffiché intégralement à chaque événement (`cli_cockpit.py:130-147`), O(n²) par session ; `/clear` ne libère rien.
- `context_os.py:30-32` : registre de `RLock` au niveau classe, muté à l'exécution et jamais purgé — le seul global mutable du dépôt.

**H9. Perte du message sortant final ou échec de run par collision d'idempotence.**
`channels.py:387-403` réserve l'emplacement « final » par événement ; le runtime réutilise `event.id` (`runtime.py:3145`) et les rejeux réinvoquent le handler avec le **même** `event.id` (`event_handler.py:821-837`). Une seconde réponse finale identique en clé mais différente en contenu lève `IdempotencyConflict` (`communication_ledger.py:214-215`), qui remonte par `route()` jusqu'à `on_output` **non protégé** (`runtime.py:3140`). Le commentaire `runtime.py:3119-3126` montre que le problème était connu.

**H10. Une session CLI non-TTY est impossible à interrompre.**
`orion_run.py:72-83` remplace SIGINT/SIGTERM par un poseur de drapeau (jamais `KeyboardInterrupt`). `cli_cockpit.py:1220` bloque dans `for raw in self.input:` et ne teste `self._stop` qu'**entre** deux lignes. Sur un pipe qui ne fournit plus de ligne, Ctrl+C et SIGTERM ne font rien : seul `kill -9` fonctionne. **[vérifié]**

**H11. Le budget de contexte peut supprimer la requête de l'utilisateur.**
`context_assembler.py:790-856` tente de préserver les deux ancres (requête + preuves) ; si elles ne tiennent pas, le code abandonne l'ancrage et `bounded_suffix` réduit tout le reste en supprimant les blocs les plus anciens, jusqu'au `return prefix` final (`:899`). Résultat possible : le modèle reçoit l'historique **sans la requête à traiter**, sans erreur ni marqueur. Le commentaire `:897-898` montre que le compromis est assumé (« plutôt que d'émettre une requête hors budget »). Reproduction à `total_max_chars=1200` ; les valeurs livrées (200 000) rendent le cas dépendant d'une forte pression sur le budget. **[vérifié]**

### MOYEN (sélection)

- **Doctrine `except TypeError: continue`** lors du sondage de méthodes (`cli_cockpit_backend.py:221-230`, `:633-642`) : un vrai `TypeError` levé *dans* `snapshot()` est interprété comme « mauvaise signature » et masqué.
- **Deux contrats `collect_observability`** : la table d'alias du cockpit ne résout ni `llm`, ni `subagents`, ni `task_store`, ni `retrieval_store` → 5 des 11 sections du snapshot sont `None` en production, alors que le même backend résout `model` ailleurs. Une régression de câblage serait indiscernable d'un sous-système absent.
- **SQL brut contre l'API privée d'`EventHandler`** : `_revive_failed_event_receipt` est tripliqué à l'identique dans `scheduler.py:103`, `subagents.py:111`, `teams.py:41`, chacune faisant `getattr(store, "_db")` puis un `UPDATE` direct sur `durable_events`, au lieu d'utiliser la transition publique de `durable_events.py`. Une migration de schéma casse les trois d'un coup.
- **`scheduler.py:220` + `:356-369`** : un seul `run_at` naïf (legacy) fait échouer `_as_utc`, l'`except` de `_load` renomme **tout** le fichier en `.corrupt` et repart d'un store vide — sans log.
- **Persistance des sous-agents sans `fsync`** : `subagents.py:709-729` fait `write_text` + `os.replace` sans `flush`/`fsync`, alors que `tasks.py:757-785` fsynce fichier **et** répertoire. Deux contrats de durabilité différents pour des stores qui prétendent tous deux à des « générations » durables.
- **Fuite d'identifiants** : `.env.backup*` contient tous les secrets en clair et n'est **pas** couvert par `.gitignore:1` (`git check-ignore` le confirme), alors que l'installeur VPS le crée délibérément à côté du dépôt (`orion_vps_install.py:297-299`). `GITHUB_TOKEN` est envoyé à **tous** les hôtes, y compris `raw.githubusercontent.com`, et les handlers de redirection ne valident que le schéma (`github_tools.py:145-151`, `:54-58`). Le token du bot Telegram transite dans les URL journalisables par httpx.
- **Égressions non journalisées** : `redaction_enabled=False` par défaut (`context_assembler.py:87-88`) alors que `llm_compaction_enabled` devient vrai dès qu'un compacteur est fourni ; tout échec est avalé (`:449-450`).
- **`MemoryStore` n'a aucun écrivain en production** : `put`/`forget` n'apparaissent que dans les tests, donc `search()` (`context_assembler.py:957`) retourne toujours vide — la « mémoire durable » annoncée est inerte.
- **Contrôle de révision non transactionnel** : `context_registry.py:108-129` fait SELECT puis INSERT/UPDATE sans `BEGIN IMMEDIATE` (et sans WAL), en contradiction avec sa propre docstring.
- **`CI` ne peut pas échouer sur le lint** (`ci.yml:36-39` : `ruff check . || true`), sans couverture, sans `timeout-minutes`, sans `pytest-timeout` — alors que la suite contient des attentes non bornées (`thread.join()` nu sur `Barrier(40)`, `readline()` sur un enfant qui dort 60 s). Un blocage peut consommer 360 min × 8 jobs.
- **La suite de tests écrit dans le vrai `data/`** : `AgentRuntime(llm_client=None)` crée `data/action_ledger.sqlite3` (+ WAL/SHM) et deux fichiers de verrou relatifs au cwd ; `conftest.py` n'isole ni le cwd ni le système de fichiers. Reproduit. **[vérifié]**
- **`orion_vps_install.py` et `reflection_engine.py` n'ont aucun test** ; `ReflectionEngine.reflect()` n'est appelé nulle part.

### FAIBLE (échantillon)

- Code mort après `return` : `cli_ui.py:1209-1214, 1233-1238, 1255-1257, 1953-1956` (vulture, confiance 100 %).
- `tasks.py:427-429` : `return` avant un littéral chaîne, donc inatteignable.
- ~30 fonctions publiques sans appelant, dont toute l'API de streaming (`stream_run`, `stream_text_async`, `list_models_async`) — qui porte en plus deux bugs SSE : les champs `event:`/`id:` sont parsés comme du JSON et interrompent le flux (`openrouter_client.py:1105-1124`), et un flux sans `[DONE]` est rapporté `succeeded` (`:1129-1133`).
- `CircuitBreaker` sans verrou alors que `budgets.py:3` affirme la thread-safety ; après la fenêtre de récupération, `allow()` ne remet pas `failures` à zéro, donc il se réouvre à la première erreur suivante, indéfiniment.
- `budgets.py:42-49` : seuls `calls`/`duration` peuvent déclencher — `max_tokens`, `max_cost_usd`, `max_turns` sont inertes car `openrouter_client` n'enregistre que `record(calls=1)`.
- Code de sortie `130` inatteignable depuis le script console : le mapping `KeyboardInterrupt` est sous `if __name__ == "__main__"` (`orion_run.py:1364-1369`).
- Divergences documentaires : `README.md:69-71` promet `Alt+Entrée`, le Markdown et l'alerte de requête lente — le cockpit n'implémente aucun des trois (`orion_run.py:280-313` filtre ces options) ; `docs/CLI_COCKPIT.md:15-16` nie des raccourcis clavier qui existent et sont testés.
- 0 marqueur `TODO`/`FIXME`/`HACK` dans les 137 fichiers — inhabituel à cette échelle, et cohérent avec C1 : les dettes ne sont pas tracées dans le code.

---

## 3. Motifs transverses

Quatre schémas expliquent la majorité des défauts, bien plus que les cas individuels.

**M1. Pannes silencieuses par conception.** 113 `except: pass`, zéro logging, pas de supervision des threads. Les trois boucles de travail meurent définitivement et sans trace ; `scheduler._load` détruit tout un fichier d'horaires sans un mot ; `recover_stale` rejoue sans borne et sans compteur visible. Le problème n'est pas tel bug mais le fait qu'aucun d'eux ne se signale.

**M2. Duplication qui diverge.** Le rapport transverse recense : 5 copies du verrou inter-processus, 4 `_clip`, 4 `_get`, 4 `_json` (avec des jeux d'options `json.dumps` différents — ce qui compte, car deux modules hachent des payloads sérialisés pour l'idempotence), 3 `_service` avec trois politiques d'alias, 3 `_action_target`, 2 `_tool_message`, 2 `_bounded_value`, 2 `collect_observability`, 6 `_now()` dont 3 renvoient `str` et 3 un `datetime`. Chaque divergence est un bug futur : le correctif appliqué à une copie ne l'est pas aux autres.

**M3. Frontières de confiance déclarées mais non appliquées.** `manifest.permissions` n'est que décoratif (`orion_toolbox.py:235-236`). La classification est indexée par **nom** et jamais réconciliée avec les noms réellement enregistrés par `register()` (`tool_manager.py:860-881`) : un paquet déclaré `read_only` peut enregistrer un handler mutant sous le nom `files` et hériter du classement. Le `[guidance]` d'un paquet tiers est injecté verbatim dans le prompt système (jusqu'à ~14,5 Ko), c'est-à-dire dans le canal le plus privilégié — l'intégrité vérifiée ne dit rien de l'intention. Les résultats d'outils reviennent en messages `role=tool` de première classe, hors de l'enveloppe `trust:"untrusted"` que la politique centrale est écrite pour se méfier.

**M4. Divergence code ↔ documentation.** Le cas Telegram (C2) en est l'exemple extrême. Ailleurs : `README` annonce des options CLI absentes du cockpit, `docs/CLI_V2.md` documente un adaptateur que rien n'enregistre, `TOOL_SECURITY.md` surévalue la protection (le plafond de 25 Mo porte sur les octets **compressés**, sans limite d'expansion → zip bomb), `docs/ORION_COMMUNICATION.md` décrit un bootstrap supprimé du code. Le dépôt contient d'excellents commentaires expliquant *pourquoi* le code a durci son comportement — et la documentation n'a pas suivi.

---

## 4. Corrections prioritaires

| Priorité | Action | Référence |
|---|---|---|
| 1 | Introduire un vrai logging (+ `on_error` sur les boucles de travail) et une supervision qui redémarre ou signale un thread mort | C1, H1 |
| 2 | Écrire `bootstrap_pairing_secret_env` dans l'installeur VPS **et** corriger `orion.toml`/README : soit documenter que l'appairage est obligatoire, soit restaurer un bootstrap explicite | C2 |
| 3 | Trancher sur les approbations : activer `[approvals]` par défaut, ou documenter que `tools.policy.privileged` ne protège rien sans elle | H2 |
| 4 | Plafonner `Retry-After` et rendre l'attente interruptible (copier le motif de `channel_adapters.py:1842-1843`) ; agréger l'usage de toutes les tentatives | H3, H4 |
| 5 | Élaguer `_durable_receipts_by_event_id` et borner `_dead_letters` ; politique de rétention sur `actions` et `communication_events` | H8 |
| 6 | Corriger le retrait du reçu dans `_process_event` et router les trois `_revive_failed_event_receipt` vers une méthode publique de `DurableEventStore` | H1, duplication |
| 7 | Supprimer ou câbler les 5 modules orphelins | H7 |
| 8 | `except: pass` → log au niveau debug ; rendre `pytest` hermétique au cwd ; rendre le lint bloquant en CI | C1, tests |

---

*Rapport produit par analyse parallèle multi-agents puis contre-vérification directe. Les constats marqués **[vérifié]** ont été relus dans le code source par l'agent coordinateur ; deux affirmations d'agents ont été corrigées après vérification (`max_attempts` est bien appliqué dans `event_handler.py:826` ; la « seconde copie du dépôt » référencée par pip est un lien mort vers un dossier inexistant, sans effet de masquage).*

---

# 5. Remédiation (état au terme du chantier)

**Hors périmètre, à la demande explicite :** C1 (journalisation) et la partie H2 qui touche le *défaut d'activation* des approbations. Ces deux points restent **ouverts**.

## Corrections appliquées

| Réf. | Correction | Fichiers |
|---|---|---|
| **H1** | Les trois boucles de travail ne meurent plus : `_worker` enveloppe `_worker_loop` dans `try/except BaseException` ; `_recover_durable`/`_hydrate_durable_queue` sont appelés via des enveloppes sûres ; `subagents._worker` capture et marque le job en échec (nouveau `_fail_job`, désormais partagé avec le handler interne) au lieu de laisser le job bloqué en RUNNING ; `teams._poll` isole chaque phase (`_poll_phase`) et journalise dans `_poll_errors`. | `event_handler.py`, `subagents.py`, `teams.py` |
| **H3** | L'usage des tentatives retentées est **cumulé** (`_accumulate_attempt_usage`) au lieu d'être écrasé, avec `retried_attempts` exposé dans `to_dict()` pour rendre l'écart visible. | `openrouter_client.py` |
| **H4** | `Retry-After` est plafonné par `retry_max_delay` (défaut 60 s, réglable, exposé dans `orion.toml` et validé dans `OrionConfig`) ; toute attente de nouvelle tentative est interruptible (`_sleep_before_retry` / `_async_sleep_before_retry` réveillés par `close()`). | `openrouter_client.py`, `orion_config.py`, `orion.toml` |
| **H6** | `_durable_lock` n'est plus tenu pendant le `queue.put` bloquant : l'id est réservé sous verrou puis le `put` a lieu hors verrou, avec libération en cas de `Full` ; le backoff n'est plus dormi dans le thread worker sur le chemin durable (le reçu est requeue par le store). | `event_handler.py` |
| **H8** | Fuites bornées : `_dead_letters`/`_callback_errors` en `deque(maxlen=…)` avec index d'éviction ; `_durable_receipts_by_event_id` libéré après traitement ; purge de rétention sur `actions` et `communication_events` (index dédié, fenêtre 7 j, jamais les lignes `running`/`uncertain`/`queued`). | `event_handler.py`, `action_ledger.py`, `communication_ledger.py` |
| **H10** | La session non-TTY est interruptible : stdin est lu par un thread daemon alimentant une file, et la boucle attend avec `get(timeout=0.2)` en testant `_stop` à chaque tour. | `cli_cockpit.py` |
| **C2** | `telegram_pairing_secret_env` est désormais transmis par l'installeur VPS (le 10ᵉ paramètre positionnel était omis), un secret est généré et écrit dans `.env` avec la même discipline de *quoting* que `orion-install`, affiché une seule fois ; `_env_values` gère le préfixe `export ` ; `orion.toml` et la docstring du script ne promettent plus un bootstrap par premier message. | `orion_vps_install.py` |
| **—** | Le fichier d'horaires survivre à une ligne corrompue : chargement ligne par ligne, `load_errors` expose les rejets, quarantaine seulement si plus rien n'est récupérable ; horodatages normalisés en UTC (`_parse_utc`) pour supprimer le `TypeError` de `snapshot()`. | `scheduler.py` |
| **—** | Le scan de reprise des réveils échoués est filtré **en SQL** (`idempotency_key_prefix`, échappement `LIKE`), au lieu de charger 10 000 reçus puis filtrer en Python à chaque poll. | `durable_events.py`, `scheduler.py` |
| **—** | `--force` ne détruit plus le durcissement : `[tools.policy]`, `[approvals]`, `[gateway]` et les allowlists de canaux sont reprises de l'ancienne configuration, sans dupliquer une table déjà générée. | `orion_install.py` |
| **—** | `.env.backup`/`orion.toml.backup` (secrets en clair) sont ignorés par git, ainsi que `.env.*` ; la règle `tests/` + `!tests/` est simplifiée et laisse de nouveau suivre les fixtures non-`.py` et les sous-dossiers. | `.gitignore` |
| **—** | Unité systemd : `StartLimitIntervalSec`/`StartLimitBurst` déplacés dans `[Unit]` (ignorés dans `[Service]` depuis systemd v230) ; `ProtectHome=read-only` (au lieu de `true`) quand l'installation est sous `/home`, `/root`, `/run/user` ou `/Users`. | `orion_vps_install.py` |
| **—** | Parseur SSE conforme : seuls les champs `data` alimentent la charge utile, les `data:` multi-lignes sont concaténés, `event:`/`id:`/`retry:` et commentaires sont ignorés ; un flux clos sans `[DONE]` est signalé comme erreur au lieu d'être compté comme un succès. | `openrouter_client.py` |
| **—** | Le repli de compatibilité ne se déclenche plus sur le message générique « Provider returned error » : un 400 qui ne désigne pas le champ optionnel ne rejoue plus la génération (double facturation) et ne masque plus la cause réelle. | `openrouter_client.py` |
| **—** | Hygiène : `build/` et `openrouter_agent_client.egg-info/` ne sont plus suivis (49 fichiers) ; la CI a un `timeout-minutes: 30` et le lint est **bloquant** (`ruff check .` sans `|| true`) avec un jeu de règles explicite (`F`, `E9`, `B`) dans `pyproject.toml` ; la suite de tests s'exécute dans un cwd privé (`tests/conftest.py`) et n'écrit plus dans le `data/` du dépôt. | `.github/workflows/ci.yml`, `pyproject.toml`, `tests/conftest.py`, `tests/test_installers.py` |

## Corrections des défauts révélés par les tests

Deux tests verrouillaient le comportement défectueux et ont été corrigés — le code est la référence :

- `tests/test_openrouter_tool_protocol.py` affirmait qu'un 400 générique déclenche le repli (c'est exactement le défaut) ; il vérifie maintenant qu'un 400 **nommant le champ** le déclenche et qu'un message générique ne le déclenche pas.
- `tests/test_subagent_parent_continuation.py:635` attendait `"terminal":2` alors que le snapshot exclut volontairement le job courant (contrat confirmé par `test_related_subagent_snapshot_excludes_current_event_job_but_stays_authoritative`, qui passe) ; l'attente est ramenée à `"terminal":1`.

## Points ouverts (non traités)

- **C1 — journalisation.** Toujours aucune trace en production : ~113 `except: pass` et zéro `logging`. C'est le point le plus rentable du rapport et il reste entier.
- **H2 — activation des approbations.** `ApprovalConfig.enabled = False` par défaut, hors périmètre demandé. À noter : les approbations n'expirent toujours pas et ne sont pas à usage unique (`runtime.py:4297-4342`).
- **H5 —** `max_attempts` ne borne toujours pas le rejeu après crash.
- **H7 —** les 5 modules orphelins (`outbox`, `workflows`, `identity_resolver`, `cli_cockpit_events`, `cli_v2`, ~1 050 lignes) sont toujours présents.
- **H9 —** la collision d'idempotence sur la sortie finale n'est pas corrigée.
- **H11 —** le budget de contexte peut toujours supprimer la requête de l'utilisateur (`context_assembler.py:899`) ; le compromis est documenté dans le code mais reste silencieux.
- **Divers :** doppelgängers non fusionnés (5 verrous, 4 `_clip`, 6 `_now()`), `channel_adapters` lit toujours le corps avant auth, l'adaptateur e-mail peut toujours mourir silencieusement, `poll_timeout > api_timeout` reste accepté, l'option `channels.cli.slow_request_seconds` reste ignorée par le cockpit, et la couverture de tests de `orion_vps_install.py`/`reflection_engine.py` est toujours nulle.

## Vérification

| Contrôle | Avant | Après |
|---|---|---|
| `pytest -q` (hors ligne) | 759 réussis, **4 échecs** | **778 réussis, 0 échec**, 4 ignorés |
| `ruff check .` | non bloquant (`\|\| true`) | **passe**, et bloquant en CI |
| `compileall` | OK | OK |
| Cwd pollué par les tests | `data/` créé dans le dépôt | **aucun** `data/` créé |
| Fichiers d'artefacts suivis | 49 | **0** |
| Vérifications bout-en-bout | — | préservation `--force`, unité systemd, appairage Telegram, interruptibilité CLI, plafond `Retry-After`, parseur SSE (tous rejoués) |

Total : **27 fichiers modifiés, +1 113 / −238 lignes**, plus 49 fichiers désuivis, et deux nouvelles suites de régression (`tests/test_audit_regressions.py`, `tests/test_cli_cockpit_interrupt.py`).

---

# 6. Écart de coût TUI vs OpenRouter (enquête + corrections)

**Symptôme :** l'en-tête du cockpit affiche `$0.0051816` alors que le tableau de bord OpenRouter affiche `$0.0263` (rapport ≈ 5,1×).

## Conclusion principale : c'est d'abord un écart de **portée**, pas un bug de calcul

`UsageLedger` est un simple dictionnaire en mémoire, **jamais persisté** (`openrouter_client.py:218-222`), construit une seule fois par processus (`orion_config.py:1036-1047`). Il n'existe ni `reset()`, ni fichier d'usage sur disque.

- **TUI** = somme des `usage.cost` rapportés par le fournisseur, pour les appels de **ce processus uniquement** ; remise à zéro à chaque redémarrage.
- **Tableau de bord** = cumul **du compte** sur la période, persistant, incluant les sessions précédentes, les redémarrages et tout autre trafic utilisant la même clé.

Le nombre affiché est bien le total de session (toutes étapes confondues : décision, réflexion, sous-agents, compaction — ils partagent tous le même client), et **aucun arrondi** n'est appliqué : `str(Decimal("0.0051816"))` produit exactement la chaîne observée, ce qui identifie formellement le chemin du cockpit (et non le bandeau de `cli_ui`, qui aurait affiché `$0.0052`). L'arithmétique est donc juste ; c'est la fenêtre temporelle qui diffère.

## Deux défauts comptables réels, corrigés

1. **[CORRIGÉ] L'accumulation des tentatives était détruite.** `_accumulate_attempt_usage` additionnait bien, mais `_finish_usage` **assignait** ensuite `prompt_tokens`/`completion_tokens`/`total_tokens` et `cost_usd` depuis la dernière réponse — annulant l'accumulation. Toute génération produite puis retentée était facturée par le fournisseur mais absente du registre. C'était une régression introduite par la correction H3 elle-même.
2. **[CORRIGÉ] Les appels facturés mais échoués disparaissaient.** `_snapshot_locked` ne totalisait que `status == "succeeded"`, et le dernier essai d'une série de reprises (ainsi que le chemin asynchrone) n'accumulait rien. Le coût d'un timeout, d'un 5xx ou d'un flux interrompu était donc perdu.

Correctifs : `_finish_usage` additionne désormais via `_accumulate_attempt_usage` ; `_payload_usage` centralise l'extraction (jetons + coût, `usage.cost` ou `cost` racine) ; le chemin asynchrone accumule comme le synchrone ; le dernier essai d'une série retryable conserve son usage ; `known_cost_usd` inclut les enregistrements `failed`/`canceled` facturés, avec une ligne distincte `abandoned_cost_usd` pour que l'opérateur voie pourquoi le total bouge sans réponse réussie.

**Couverture de test :** le test existant n'exerçait que le helper isolé — c'est précisément pour cela que la régression est passée inaperçue. Trois tests pilotent maintenant `complete()` de bout en bout à travers un vrai retry (503 facturé puis 200), via `httpx.MockTransport`.

## Points laissés ouverts

- **Persistance de l'usage — écartée sur décision.** Le registre reste en mémoire ; l'en-tête affiche donc désormais explicitement la portée : `$0.0051816 this session`, plus `· N unpriced` lorsqu'un appel réussi n'a pas retourné de coût (le marqueur « ? » que `cli_ui` affichait déjà). La formulation se dégrade avec la largeur (`… this session` → montant seul) mais le montant n'est jamais supprimé ; le coût passe avant le nom du modèle dans le budget de largeur. Le compteur « N events » a été retiré de l'en-tête (redondant, consultable via `/status`).
- **`budgets.max_cost_usd` reste inerte** — non traité, sur décision explicite.
- `estimated_cost_usd` reste définitivement nul (`cost_source == "catalog_estimate"` n'est jamais écrit).

---

# 7. TUI : lisibilité, couleur et défilement

- **Couleur par rôle.** Le transcript est coloré via un `Lexer` attaché au `BufferControl` du `TextArea` — la couleur est donc appliquée sans remplacer le widget, ce qui préserve l'API du buffer (et les ~40 assertions existantes). Palette : `YOU` en bleu, `ORION` en vert, sous-agents en violet, notifications en gris italique, corps de message en gris clair.
- **Barre de défilement** réelle sur le transcript, pour que la position de lecture soit visible.
- **Indicateur « SCROLLED BACK »** dans l'en-tête + `history · End to follow live` dans le pied, avec budget de largeur (à 32 colonnes l'alerte est prioritaire sur la liste de touches).
- **Ancrage de la vue** par cellule (ligne, colonne) et non par offset brut, ce qui survit à une modification du texte au-dessus de la position de lecture (compaction, `/clear`).
- **Rendu borné** à `_MAX_RENDERED_EVENTS = 400` blocs (le journal complet reste dans `transcript_events`), et reconstruction ignorée quand le texte est identique.

**Bug trouvé dans mon propre correctif :** la détection du titre se faisait par correspondance de texte, si bien qu'un corps de message contenant une ligne `ORION` voyait son titre stylé comme du corps. La classe est désormais décidée **par position** (première ligne du bloc), et la carte de styles est construite et vérifiée pour être exactement alignée sur le texte.

**Non reproduit :** la dérive de défilement et le chevauchement de texte signalés par l'utilisateur n'ont pas pu être reproduits dans le harnais de rendu (contenu visible identique avant/après l'arrivée de sortie, sur six combinaisons de défilement). Le correctif d'ancrage traite une faiblesse réelle mais **n'est pas confirmé** comme étant la cause du symptôme observé.

---

# 8. Enquête : Orion figé après une mise en veille

**Symptôme :** une grosse tâche en cours (~10 sous-agents), PC mis en veille, puis au réveil l'interface accepte encore la saisie (les messages apparaissent dans le transcript) mais l'agent ne répond plus jamais. `STOP TOUT DE SUITE` (texte libre) n'a évidemment pas interrompu.

Quatre enquêtes parallèles (horloges/baux, chemin des messages, interblocages, réseau) ont convergé sur le **même** premier suspect.

## Cause principale — le thread `agent-runtime` mourait en silence (CORRIGÉ)

`runtime._run` (`runtime.py:5503-5526`) était la **seule** boucle de travail du dépôt sans garde d'exception : ni `try` autour de `_recover_durable_inbox`/`_hydrate_durable_inbox`, ni autour de `_process_runtime_event`. Un `sqlite3.OperationalError` (rafale d'écritures au réveil) ou toute exception hors bande tuait le thread définitivement — sans journal, sans redémarrage, sans `on_error`. Les trois autres boucles (`event_handler`, `subagents`, `teams`) avaient déjà été durcies, ce qui rendait l'asymétrie d'autant plus visible.

Tant que `_run_in_progress` restait vrai, **chaque nouveau message partait dans `_deferred_events`** et n'était promu qu'au retour à `_run_in_progress == False` (`runtime.py:5507`) : plus rien ne répondait, alors que l'interface continuait d'accepter et d'acquitter les messages. C'est exactement la signature observée.

Correctifs : boucle supervisée (`_run`/`_run_loop` + `finally` qui remet `_run_in_progress` à faux), garde par itération sur la maintenance et sur le traitement d'un événement, erreurs conservées dans `_runtime_errors` et remontées via `on_error`, et `_wake` capture désormais `BaseException` (un `KeyboardInterrupt`/`SystemExit` échappant d'un outil laissait sinon `_run_in_progress` bloqué à vrai pour toujours).

## Cause secondaire — aucune commande ne pouvait arrêter un run (CORRIGÉ)

- Un message tapé n'est **jamais** un interrupt : `_should_preempt` exige une priorité *strictement* supérieure, et un texte CLI vaut `NORMAL` (20) comme l'événement du run → `20 > 20` est faux. Il est donc toujours différé.
- Il n'existait **aucune** commande `/cancel`, `/stop` ou `/interrupt` dans le cockpit. `/stop` (documenté au README:208) n'existe que dans l'ancien backend de `orion_run` et ne fait qu'annuler l'affichage de la requête — sa docstring le dit : « without stopping Orion's durable runtime worker ». Seul Ctrl+C quittait la session.

Correctif : `AgentRuntime.request_cancel()` (réutilise les points d'interruption existants, donc arrêt entre deux appels, jamais au milieu d'un effet), exposé par `/cancel` dans le cockpit et documenté dans l'aide. Le signalement précise que l'appel en cours doit d'abord se terminer.

## Cause contributive — attentes non bornées (CORRIGÉ partiellement)

- **Timeout httpx par phase** : passer un seul flottant à httpx applique 60 s à *chaque* phase (connect/read/write/pool), soit jusqu'à 180 s par tentative — et une connexion demi-ouverte après réveil consomme de nouvelles fenêtres à chaque reprise. Les phases rapides sont désormais bornées à 15 s.
- **Aucune échéance totale** : httpx n'en fournit pas. Ajout de `llm.request_deadline` (150 s par défaut), qui libère l'appelant même si le socket reste bloqué. Vérifié : un gestionnaire qui ne répond jamais rend la main en 2,02 s au lieu de 180 s+.
- **`max_runtime_seconds` (900 s) n'est vérifié qu'en haut de boucle** (`subagents.py:2271-2273`), donc jamais pendant un appel bloqué : une seule requête peut le dépasser. Non traité.

## Découvertes non corrigées

- **`time.monotonic()` n'est pas sûr face à la veille sous Windows** : CPython utilise `GetTickCount64`, qui *inclut* le temps de veille (PEP 418). Toutes les échéances « monotones » se déclenchent donc au réveil également ; le partage mur/monotone n'apporte ici aucune protection.
- **Baux à l'horloge murale** : au réveil, toutes les réservations `running` de l'ActionLedger deviennent `uncertain` (`action_ledger.py:236-244`), un outil réellement réussi est déclaré non fiable et n'est jamais rejoué ; les reçus durables en `processing` perdent leur fence alors que l'effet de bord a déjà eu lieu.
- **Embouteillage de reprise** : reprise massive des reçus, des baux et des horaires échus en une passe, sans plafond — c'est précisément ce qui a pu déclencher l'erreur SQLite initiale.
- **Cockpit aveugle** : `runtime.pending_events` / `pending_events_during_run` existent (`runtime.py:598-607`) mais aucun code du cockpit ne les lit ; l'instantané n'expose que `{state, running}`. Un run mort s'affiche donc toujours `RUN`. Câblage mort, à raccorder pour rendre ce type de panne détectable.
- **Fin de reprise de verrou fantôme** (CORRIGÉ) : `acknowledge_delivery` ne libérait `_published_ids` que si son `UPDATE` modifiait une ligne. Après expiration du bail (5 s), la ligne ne correspond plus et l'identifiant restait marqué publié, donc le message n'était plus jamais livré et le parent n'obtenait jamais `handoff.completed`.

## Réponse directe à la question

**Oui, la veille est très probablement le déclencheur — mais pas la cause profonde.** Elle a cassé les connexions en vol et provoqué une rafale de reprises ; c'est cette reprise qui a fait tomber la boucle non supervisée. La veille seule ne suffit pas (les baux expirent et se soignent en ≤ 300 s, les plafonds de reprise existent) ; c'est l'absence de supervision qui a transformé un incident transitoire en blocage définitif.

## Vérification

| Contrôle | Résultat |
|---|---|
| `pytest -q` | **842 réussis, 0 échec**, 4 ignorés |
| `ruff check .` | passe |
| Nouveaux tests | `tests/test_runtime_hang_regressions.py` (8) + 1 test d'affichage d'état |
| Échéance totale | gestionnaire bloquant libéré en 2,02 s (au lieu de 180 s+) |
| `/cancel` | atteint le runtime via le cockpit et via `cli._command`, `no_active_run` correctement signalé |

---

# 9. Retours TUI : couleurs, notification worker, troncature

## Couleurs — palette blanc/gris/orange (CORRIGÉ)

Deux défauts distincts :

1. **Sortie de commande colorée comme du chat.** `_render_result` écrit le résultat dans la même vue que le transcript, mais `_set_view` ne publiait aucune carte de styles : le lexer conservait donc les classes de rôle de la vue précédente. D'où, par exemple, un `/jobs` rendu en couleur « corps de message ». `_set_view` dérive désormais la carte selon le mode (`class:transcript.view` pour tout ce qui n'est pas le chat) et `_set_view(..., mode="chat")` reconstruit les rôles.
2. **Palette bleu/vert remplacée** par du blanc/gris/orange : `ORION` et les workers en orange (`#ff9e64` / `#ffb86b`), corps de message quasi blanc (`#e6edf3`), saisie utilisateur et notifications en gris (`#8b949e`), sortie machine en gris neutre (`#b9c0c9`). Les décorations `reverse` de l'en-tête/pied ont été retirées au profit d'accents orange, plus lisibles selon les thèmes de terminal.

La sortie de commande utilise en outre le rendu lisible fourni par le backend (`display`) au lieu d'un dump JSON brut, avec un repli `_format_view_data` qui met en forme listes et dictionnaires.

## Contenu worker affiché alors qu'une notification suffit (CORRIGÉ)

Cause trouvée : le runtime émet l'artefact worker depuis **deux** endroits. Le premier (`runtime.py:5025`) posait `intermediate=True` et `phase="subagent_result"`, ce qui permet au cockpit de le réduire à une notification. Le second — le chemin de synthèse finale (`runtime.py:5983`) — posait seulement `output_origin="subagent"` et `sender_name`. Le cockpit ne pouvait donc pas le reconnaître comme un artefact intermédiaire et affichait le rapport complet comme un message d'Orion.

C'est exactement la forme reproduite en test : `output_origin` seul ⇒ kind `worker` avec le rapport entier ; avec `intermediate` + `phase` ⇒ notification « résultat reçu ». Les deux chemins d'émission sont désormais alignés.

## Troncature des messages intermédiaires (CORRIGÉ)

`_intermediate_notification` coupait le texte des préambules d'Orion à 140/180 caractères, produisant des lignes se terminant par « … » et perdant l'information utile. La troncature est supprimée : ces préambules sont courts par nature. Le plafond de 700 caractères appliqué par `_emit_output` aux sorties intermédiaires a par ailleurs été vérifié — il reste en place pour les messages d'erreur d'outil, mais les résultats de sous-agents gardent leur budget complet (`response_max_chars`).

## Vérification

| Contrôle | Résultat |
|---|---|
| `pytest -q` | **847 réussis, 0 échec**, 4 ignorés |
| `ruff check .` | passe |
| Nouveaux tests | notification worker depuis le chemin final, absence de troncature, style `transcript.view` sur la sortie de commande, palette, mise en forme des données |
| Rendu vérifié | `/jobs` rendu en gris neutre, chat en rôles ; aucun vert/bleu résiduel |

---

# 10. Rendu Markdown dans le cockpit (CORRIGÉ)

Le transcript affichait le Markdown brut : `**gras**`, `` `code` ``, `## Titre`, `| Champ | Valeur |` arrivaient tels quels. L'ancienne CLI rendait déjà le Markdown via Rich (`cli_ui.py`), mais pas le cockpit.

## Solution

Un `_MarkdownRenderer` convertit le Markdown en fragments prompt_toolkit (une liste de fragments par ligne), consommés par le `Lexer` déjà en place. Aucun widget n'est remplacé, donc l'API du buffer et les tests existants restent valides. `markdown-it-py` est utilisé directement : il était déjà installé comme dépendance de Rich, et il est désormais déclaré explicitement dans `pyproject.toml` pour que la fonctionnalité ne dépende pas d'une dépendance transitive.

Pris en charge : titres (h1-h4), **gras**, *italique*, `code` en ligne, blocs de code, listes à puces **et** listes numérotées (compteur correct), citations, liens (texte seul, URL masquée), images (texte alternatif), règles horizontales, tableaux (colonnes alignées avec séparateur sous l'en-tête) et barré.

## Points d'implémentation notables

- **Le rendu dépend du mode, pas de la présence de styles.** Le `Lexer` choisit le rendu Markdown selon `_view_mode == "chat"` ; les vues machine (tableau de bord, aide, sortie de commande) gardent leur carte de styles explicite. Se fier à « y a-t-il une carte de styles ? » faisait passer le chat par la branche machine, puisque le chat publie lui aussi des styles de rôle — le Markdown n'était alors pas rendu.
- **`gfm-like` inutilisable** : ce preset charge `linkify`, un paquet optionnel absent ; tous les tokens retombaient en texte brut et le Markdown disparaissait complètement. Le parseur part de `commonmark` et active uniquement `table` et `strikethrough`, qui n'exigent aucune installation supplémentaire.
- **Règle horizontale en ASCII** : le caractère `─` n'est pas encodable en cp1252, ce qui cassait la sortie sur un terminal Windows en page de code 1252.
- **Cache d'une entrée** dans le lexer, indexé sur `(mode, texte)` : la conversion n'est refaite que si le document change réellement.
- **Repli sans parseur** : si `markdown-it-py` est absent, le texte est rendu tel quel plutôt que perdu.

Le tampon (`_view.text`) contient toujours la source Markdown — le rendu a lieu dans le lexer — donc les fournisseurs et tests qui lisent ce texte ne sont pas affectés.

## Vérification

| Contrôle | Résultat |
|---|---|
| `pytest -q` | **851 réussis, 0 échec**, 4 ignorés |
| `ruff check .` | passe |
| Nouveaux tests | rendu des marqueurs, classes par rôle, tableaux alignés, listes numérotées, repli sans parseur, vue machine non rendue en Markdown |
| Rendu écran vérifié | `**gras**` → gras, `## Fichiers` → titre stylé, `- item` → `• item`, tableau → colonnes alignées, `[lien](url)` → texte seul |

---

# 11. Rafraîchissement automatique de l'en-tête (CORRIGÉ)

L'en-tête (état du runtime, coût de session, approbations, profondeur de file) n'était recalculé que sur trois déclencheurs : la construction de l'application, l'arrivée d'une sortie, et l'exécution d'une commande. Pendant un RUN long et silencieux — typiquement plusieurs sous-agents en cours — rien de tout cela ne se produit, donc l'en-tête restait figé sur des valeurs périmées jusqu'à ce qu'un `/status` le réveille.

## Solution

Un thread de rafraîchissement (`orion-cockpit-refresh`, daemon) appelle périodiquement `_refresh_overview()` et demande un redraw via `loop.call_soon_threadsafe(app.invalidate)`. Cadence par défaut : 1 s, réglable par le paramètre de constructeur `refresh_seconds` (0 ou négatif désactive la boucle).

Le calcul est fait **hors du thread d'interface** : l'instantané du backend parcourt les manifestes d'outils installés (`ToolManager.installed()` coûte ~4,8 ms pour 6 paquets), ce que l'audit avait déjà signalé comme travail bloquant sur la boucle prompt-toolkit. Le résultat est publié sous verrou (`_snapshot_lock`) et lu par l'en-tête via `_current_snapshot()`.

## Points d'implémentation

- **Dégradation sûre** : si `backend.snapshot()` lève, l'erreur est avalée et la boucle continue — un backend défaillant ne doit pas tuer le rafraîchissement, ce qu'un test vérifie explicitement.
- **Idempotence** : `_start_refresh_loop` ne démarre pas un second thread si le premier est vivant ; `_stop_refresh_loop` est appelé dans le `finally` de `run_tui` et rejoint le thread avec un délai borné.
- **Aucune lecture concurrente non protégée** : les trois lecteurs de `_last_snapshot` passent désormais par des accesseurs synchronisés.
- **Pas d'impact sur les tests existants** : le paramètre a une valeur par défaut et n'est pas activé hors `run_tui`, donc aucune suite ne voit de thread supplémentaire.

## Vérification

| Contrôle | Résultat |
|---|---|
| `pytest -q` | **854 réussis, 0 échec**, 4 ignorés |
| `ruff check .` | passe |
| Bascule coût/état sans commande ni sortie | vérifié : `$0.111111` puis `SLEEP` + `$0.222222` apparaissent seuls |
| Boucle désactivable | `refresh_seconds=0` ⇒ aucun thread |
| Backend défaillant | la boucle survit à des exceptions intermittentes |

---

# 12. Sorties de sous-agents sur Telegram (CORRIGÉ)

Même défaut que celui corrigé dans le cockpit, mais côté canaux : les rapports des sous-agents étaient livrés sur Telegram comme des messages ordinaires, sans attribution. Ils se lisaient donc comme si Orion les avait écrits — l'utilisateur voyait « Orion : » suivi du rapport brut d'un worker.

## Cause

`TelegramAdapter.send` envoyait `output.content` tel quel, et aucun adaptateur de canal ne tenait compte de `output_origin` / `sender_name`. L'attribution n'existait que dans le cockpit (`_worker_speaker`), donc le correctif précédent — qui marque l'artefact worker avec `intermediate=True` et `phase="subagent_result"` — aidait la CLI mais restait sans effet sur les autres canaux.

## Solution

Trois helpers partagés dans `channel_adapters.py`, volontairement placés au niveau module pour être réutilisables :

- `worker_attribution(output)` — nomme le sous-agent (`sender_name`, `agent_name`, `subagent_id`, `agent_id`), ou `None` pour Orion.
- `is_intermediate_worker_artifact(output)` — vrai pour un résultat de worker qu'Orion s'apprête à synthétiser.
- `outbound_display_text(output, *, label_workers=True)` — renvoie le texte à livrer, **ou `None`** pour ne rien envoyer.

Comportement appliqué :

| Canal | Artefact worker intermédiaire | Résultat worker autonome |
|---|---|---|
| Telegram | supprimé | étiqueté `🔧 Sous-agent · nom` |
| Email | supprimé | étiqueté `🔧 Sous-agent · nom` |
| Discord | supprimé | étiqueté `🔧 Sous-agent · nom` |
| Webhook HTTP | **conservé**, étiqueté | étiqueté |
| Cockpit CLI | notification d'une ligne | en-tête `WORKER · nom` |

Le webhook fait exception délibérée : un consommateur HTTP peut attendre une livraison par sortie, donc une suppression silencieuse ressemblerait à une réponse manquante. Il est étiqueté mais jamais supprimé.

## Point de correction important

La suppression se fait en **retournant normalement**, pas en levant une exception. Le worker sortant de `ChannelRouter` acquitte la ligne du registre quand `send` retourne et la marque en échec quand une exception remonte — supprimer via une exception aurait donc rejoué le même artefact indéfiniment. Un test verrouille ce comportement.

## Vérification

| Contrôle | Résultat |
|---|---|
| `pytest -q` | **862 réussis, 0 échec**, 4 ignorés |
| `ruff check .` | passe |
| Nouveaux tests | `tests/test_worker_output_attribution.py` (8) : attribution, reconnaissance de l'artefact, suppression, étiquetage, non-régression des sorties d'Orion, adaptateur Telegram réel, absence d'exception |







