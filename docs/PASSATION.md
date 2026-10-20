# Passation — 20/10/2026

Où en est le travail sur la branche `claude/hopeful-cannon-l089w0` (PR #8), pour qui le reprend.
Les raisons de chaque choix sont dans `DECISIONS.md` ; l'architecture dans
`architecture/04-architecture.md`.

## Ce qui a été fait sur cette branche (D50 à D61)

- **D50 Photos** : Orion voit les images reçues par iMessage (modèle de vision).
- **D51 Il continue de penser** : questions ouvertes reprises la nuit, nouvelles prises après
  un événement.
- **D52 Chronique** : une page sur la semaine du propriétaire, le dimanche soir.
- **D53 Intentions** : ce qu'il veut devenir, suivi et bilan hebdomadaire.
- **D54 Secrétaire** : ses proches peuvent écrire à Orion, selon ses règles (le gardien
  vérifie chaque réponse).
- **D55 Modèles configurables** : chaque modèle utilisé se règle dans `[agent]`.
- **D56 Relations** : rendez-vous préparés, anniversaires, garder le contact.
- **D57 Rappels de lieu** : déclenchés quand il arrive ou part d'un endroit (automatisation
  iPhone).
- **D58 Ce qu'il remarque, ses goûts** : constats mensuels, goûts gardés en mémoire.
- **D59 Un outil par domaine** : les petites variantes deviennent les actions d'un même outil
  (`action=…`), de 64 à 33 outils.
- **D60 Fichiers** : image (modèle d'images), PDF (Chromium sans écran), capture du
  navigateur, envoyés au propriétaire seul.
- **D61 N'importe quel fichier** : `file` (download, server, device) : un fichier du web, du
  serveur d'Orion (jamais les secrets) ou d'un dossier partagé via `orion hand` (`get_file`).

Correctif notable : une ligne `guardian/` du `.gitignore` cachait le paquet
`src/orion/guardian/` ; les motifs sont maintenant ancrés à la racine.

## État vérifié

- `uv run pytest -q` : 355 tests passent. `uv run pyright` : 0 erreur. `ruff` : propre.
- Évaluations complètes (`python -m evals --repeat 2`) avant D60–D61 : Haiku 36/38, DeepSeek
  33/38. `fais_moi_une_image` et `envoie_moi_ce_xml` : 2/2 sur les deux.
- En vrai : une image et un PDF produits par Orion.

## Pas encore vérifié en vrai

- Un fichier qui arrive sur l'iPhone par Inkbox (`POST /media`, puis `media_urls`), et surtout
  un type quelconque (`.xml`, `.php`) : Inkbox pourrait n'accepter que certains types. Sinon,
  l'envoyer dans un `.zip`.
- Le secrétaire avec un vrai contact (déclarer les contacts dans `orion.toml`, puis
  `orion inkbox setup`).
- Les rappels de lieu de bout en bout (automatisation iPhone à créer).
- Les constats mensuels (il faut trois semaines de journal).

## Connu et non corrigé

- Cas d'évaluation `demain_a_1h_du_matin` : à 1 h du matin, « demain » est compris comme le
  lendemain du jour civil ; il devrait demander ou choisir le jour même. Échoue sur les deux
  modèles.
- DeepSeek : parfois trop long (`salut`), une pointe d'ironie (`un_truc_avec_paul`), et lance
  parfois `computer_task` quand `browser` suffit (`appart_a_dreux`).
- Le tableau de bord Inkbox contient encore des règles de contact d'exemple pour
  +33600000000 : le propriétaire doit les supprimer.

## Faire tourner Orion

`orion.toml` et `data/` ne sont pas dans le dépôt (numéro du propriétaire, journal) : partir de
`orion.example.toml`. Les secrets passent par des variables d'environnement (`SecretRef`).

```
uv sync
uv run orion doctor
uv run orion guardian            # garde les secrets (INKBOX_API_KEY…)
env -u INKBOX_API_KEY uv run orion serve   # le cœur, qui n'a que la clé du modèle
```

En production (OVH), voir `deploy/` : deux services systemd, `orion-guardian` et
`orion-core`, sous deux utilisateurs distincts.
