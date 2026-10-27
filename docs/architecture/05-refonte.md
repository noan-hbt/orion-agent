# Refonte d'Orion — audit et plan (27/10/2026)

Le propriétaire veut un Jarvis : un deuxième cerveau qui s'imbrique dans sa vie, le comprend
comme un meilleur ami, le guide, s'en préoccupe. Ce qu'il constate aujourd'hui : peu de
curiosité, d'intuition et d'autonomie ; des choses pas menées au bout, ni réfléchies jusqu'au
bout ; pas de recul ; une mémoire qui garde du temporaire et rate l'important ; des messages
au format mal choisi ; le mauvais outil pour une demande (une mission toutes les 6 h pour ce qui
méritait une réponse tout de suite) ; des fonctionnalités mal utilisées, et beaucoup qui
manquent. Pas assez fiable, pas assez capable.

Cet audit cherche pourquoi dans le code, pas dans les consignes.

## 1. Ce qu'est Orion aujourd'hui

- 50 600 lignes de Python, 42 modules intégrés, 148 décisions.
- **Un seul agent réactif.** Chaque message du propriétaire lance une boucle : le modèle du jour
  lit un message système de 43 400 caractères (la constitution et une section par module) et
  57 outils (47 700 caractères de schémas). Environ 25 000 jetons fixes avant le premier mot.
- **La proactivité est une somme de règles.** Vingt sources d'alertes indépendantes (agenda,
  mail, missions, relations, papiers, intentions, curiosité…) décident chacune, avec sa propre
  règle codée, qu'il y a quelque chose à dire. Le moteur d'initiative trie ces alertes (seuil,
  points du jour, budget), puis le modèle les met en mots.
- **La réflexion a lieu une fois par nuit** (D117) : un passage avec un modèle fort relit la
  journée et avance ce qui traîne. Le jour, personne ne prend de recul.
- **La mémoire est une liste de faits** : une phrase, des noms, une catégorie (profil, goût,
  personne, projet, autre), une date de début. Deux écrivains : le modèle pendant la
  conversation (`remember`) et l'extracteur après (D86). Un portrait réécrit la nuit (D144).

## 2. Diagnostic

### 2.1 Les consignes s'empilent, le modèle s'y noie

Chaque travers constaté a reçu sa consigne : la constitution est passée de 34,7 k à 43,4 k
caractères en une semaine, et chaque module ajoute sa section. Un petit modèle (Haiku,
DeepSeek, celui du forfait) lit 25 000 jetons de règles, d'exemples et de schémas avant chaque
réponse ; les plus récentes chassent les autres. Les évaluations le montrent : corriger un
travers par une phrase en déplace un autre (« salut » à 1/2 après la consigne d'intuition,
D146), et une consigne abstraite ne marche pas sans exemple (« la salle de sport », 0/4).

C'est la cause racine de « ses fonctionnalités ne sont pas correctement utilisées » : il y en
a trop, décrites trop longuement, au même niveau.

### 2.2 Dix façons de faire « plus tard »

Pour remettre quelque chose à plus tard, le modèle choisit entre `remind_me`, `follow_up`,
`think_later`, `awaiting`, `task`, `mission`, `commitments`, `departure`, `relations`,
`booking` et `intention`, sans compter `planned` et `reminders` pour les voir. Chacun a ses
règles. D'où « une mission toutes les 6 h pour ce qui méritait une réponse tout de suite » :
rien dans le code ne distingue ce qui se fait maintenant de ce qui se suit, et remettre à plus
tard est toujours la voie la plus facile pour un modèle.

### 2.3 Aucune vision d'ensemble de sa vie

Orion connaît des faits, mais pas des **situations** : « il cherche un appart à Lyon d'ici
janvier », « Léa est malade cette semaine », « examen le 12, il stresse ». Ces choses ont un
début, une fin, des liens (son agenda, ses proches, ses missions), et ce sont elles qu'un ami
garde en tête. Aujourd'hui elles sont soit des faits « durables » qui ne s'éteignent jamais
(d'où le temporaire qui s'accumule), soit perdues parce que l'extracteur ne garde que « ce qui
vaudra encore dans un mois » (d'où l'important raté).

### 2.4 L'intuition est éclatée en vingt règles

Une relation qui n'a pas eu de nouvelles depuis trois mois, un papier qui expire, un mail
important : chaque source décide seule, sans voir les autres ni l'ensemble. Personne ne se
demande : « avec tout ce que je sais de lui aujourd'hui, qu'est-ce qui compte, qu'est-ce que je
devrais préparer, qu'est-ce qui va coincer ? ». C'est pourtant la définition de l'intuition et
du recul. La nuit le fait une fois, après coup, sur la conversation seulement.

### 2.5 La mémoire n'a pas de temps

Un fait n'a pas d'horizon : « il est à Lisbonne ce week-end » et « il est allergique aux
noix » ont le même statut. Le portrait s'écrit sur les 400 faits les plus récents. Le doute
(D83) et la nuit retirent des doublons, mais rien ne fait mourir ce qui était temporaire.
Rien non plus ne garde les **épisodes** (ce qui s'est passé, ce qu'il a ressenti) autrement
que dans la conversation brute.

### 2.6 Le format est laissé au modèle, canal par canal

Le modèle écrit les balises de l'application (D126) ou du texte, selon des consignes, sans
savoir toujours où part son message. La longueur, la structure (une réponse, une liste de
trains, un résumé de mails) ne dépendent que de la consigne.

### 2.7 La fiabilité ne se mesure pas sur sa vraie vie

54 cas d'évaluation, écrits à la main, après coup. Rien ne remonte ce qui rate en vrai : une
réponse qu'il corrige, un « non c'est pas ça », une tâche abandonnée. Les erreurs vues en vrai
arrivent par lui, en conversation avec moi.

### 2.8 Ce qu'il ne voit pas de sa vie

Orion voit : ses messages, son agenda, ses mails, ses papiers, les fichiers des dossiers qu'il
partage, le web. Il ne voit pas : où il est (le module `places` attend un téléphone réglé),
sa santé (sommeil, pas, sport), son argent (comptes, dépenses, abonnements), ses contacts, ce
qu'il écoute, ses notes, ses trajets et leurs perturbations, ses colis, sa maison. Un Jarvis
« omniscient » commence par ces sources.

## 3. Le plan

Six chantiers, dans cet ordre : chacun sert le suivant, et chacun se livre seul, testé, avec
des évaluations avant et après.

### Chantier 1 — Les situations : le fil de sa vie (mémoire)

Un nouvel objet de mémoire, à côté des faits : la **situation**, ce qui se passe dans sa vie en
ce moment. Un titre, ce qu'on en sait, les gens et les dates qui s'y rattachent, un horizon
(« jusqu'au 12 novembre », « tant qu'il cherche »), un état (en cours, réglée, abandonnée).
- L'extracteur sépare : fait durable, situation (ouvrir, compléter, clore), engagement. Le
  temporaire ne devient plus un fait.
- Une situation passée son horizon se ferme seule la nuit ; une situation sans nouvelles
  depuis longtemps est posée en question.
- `<maintenant>` montre ses situations ouvertes, en plus de l'agenda et des échéances ; le
  portrait s'écrit à partir d'elles.
- Les faits existants de nature temporaire sont reclassés une fois, par de nouveaux événements
  (le journal ne change pas).

### Chantier 2 — La veille : réfléchir le jour, pas seulement la nuit (intuition, recul)

Un passage de réflexion plusieurs fois par jour (par défaut vers 7 h 30, 12 h 30, 18 h 30, et
après un événement qui compte : un mail important, un agenda qui bouge), avec le modèle fort,
qui regarde **l'ensemble** : ses situations, son agenda des 48 h, ce qui arrive à échéance, les
signaux des vingt sources (devenus des informations, plus des messages tout faits), la
conversation récente. Il décide : rien, travailler en silence (chercher, préparer, lancer une
tâche), le lui dire maintenant, ou le garder pour le prochain point. C'est là que naissent les
liens (« tu pars à Lisbonne vendredi et ton passeport expire en décembre »), les anticipations
et les idées. La nuit reste, pour le travail long.

### Chantier 3 — Moins d'outils, mieux choisis (utiliser ce qu'il a)

- **Un seul « plus tard »** : un outil `suivi` qui dit quand (un moment, un rythme, une réponse
  attendue) et quoi faire alors (lui dire, lui demander, y travailler). Le code choisit le
  mécanisme (rappel, suivi, mission, tâche, attente). Les anciens outils restent lisibles dans
  le journal ; ils disparaissent seulement de ce que voit le modèle.
- **Faire maintenant d'abord**, vérifié dans le code comme D144 l'a fait pour les promesses :
  un tour du propriétaire qui ne fait que lancer un suivi, sans avoir rien cherché alors que
  la demande se traitait tout de suite, revient une fois au modèle.
- **Les outils rares hors du menu** : ce qui sert une fois par mois (`provenance`,
  `contain_source`, `autonomy_*`, `module_*`, `console_link`, `spending`…) passe derrière un
  outil `orion` qui les liste et les appelle. Objectif : une trentaine d'outils, 25 k
  caractères de schémas.
- **Une constitution réécrite courte** : ce qu'Orion est et sa façon d'être (une page), le
  reste dans les descriptions d'outils, là où le modèle le lit au moment de s'en servir.
  Objectif : moins de 25 k caractères.

### Chantier 4 — La fiabilité mesurée sur sa vraie vie

- **« C'est raté »** depuis l'application (un appui long sur un message) et repéré dans ses
  mots (« non », « c'est pas ça », « tu t'es trompé ») : le tour est gardé, avec ce qui a
  raté, dans un carnet que je peux relire.
- `orion evals export` transforme un raté en cas d'évaluation, sans ses données personnelles
  à la main.
- Le relevé de la semaine : tours arrêtés par une limite, erreurs d'outils, réponses
  corrigées, coût. Il me sert à choisir le chantier suivant.

### Chantier 5 — Le bon format, décidé par le canal

Le modèle écrit une seule fois, dans une forme simple (du texte, et des pièces : un choix, une
fiche, un rendez-vous, une action faite). Chaque canal la rend à sa façon : l'application
dessine, Telegram met du texte propre, un vocal lit la phrase. Les consignes de format
quittent le message système.

### Chantier 6 — Ce qu'il voit de sa vie (capacités)

Par ordre d'utilité pour le coût :
1. **Ses raccourcis iPhone** : une adresse signée où les Raccourcis d'iOS envoient ce que le
   téléphone sait (arrivé au travail, parti de chez lui, sommeil, pas, batterie, mode
   concentration, morceau en cours). Sans application native, c'est ce qui donne le plus.
2. **Ses contacts** (CardDAV, ou un export du téléphone) : les proches avec leurs dates.
3. **Ses trajets** : perturbations SNCF et RATP sur ses trajets habituels, temps de trajet
   réel avant un rendez-vous.
4. **Ses colis et abonnements**, tirés de ses mails : arrivées, renouvellements, essais
   gratuits qui deviennent payants.
5. **Son argent** : un agrégateur bancaire (GoCardless Bank Account Data, gratuit pour un
   particulier) en lecture seule, dans le gardien : dépenses, prélèvements inhabituels, fin
   de mois.
6. **Sa maison**, par Home Assistant (le module MCP existe déjà).
7. **Ses lectures** : flux RSS et sujets suivis, branchés sur la curiosité.

## 4. Ce qui ne change pas

Le journal en ajout seul, les projections reconstruites, la provenance et la confiance, le
gardien, l'outbox, le contrat du cache de prompt. La refonte change ce qu'Orion pense et
voit, pas les garanties qui le rendent sûr.

## 5. Où on en est (27/10/2026)

| Chantier | État | Décision |
|---|---|---|
| 1. Les situations | fait | D149 |
| 2. La veille | fait, à heures fixes et quand un mail ou l'agenda bouge | D150, D155 |
| 3. Moins d'outils | tiroir et « faire maintenant » faits ; un seul « plus tard » et constitution courte pas faits | D151 |
| 4. La fiabilité mesurée | `orion misses` fait ; relevé de la semaine pas fait | D152 |
| 5. Le format par canal | déjà l'architecture (D126) : rien changé | — |
| 6. Ce qu'il voit de sa vie | son téléphone fait ; contacts, trajets, colis, banque, maison, flux à faire | D153 |

## 6. Mesure

Évaluations complètes sur Haiku 5.5, un passage par cas : 47/54 avant la refonte, 49/57
après les chantiers 1 à 3 (les trois cas ajoutés passent). Deux cas réparés
(`la_nuit_reprend_ce_qui_reste`, `va_au_bout_au_lieu_de_proposer`), deux nouveaux échecs
(`un_lien_sans_un_mot`, `relance_le`) qui, rejoués trois fois, passent 2/3 ici comme sur
`main` (1/3 et 2/3) : du bruit, pas une régression. `appart_a_dreux` dépend du web réel,
bloqué dans l'environnement de mesure. Le reste des échecs est le même qu'avant
(`demain_a_1h_du_matin`, `il_etait_coupe`, `veille_de_partiel`, `un_resto_ce_soir`,
`il_a_deja_repondu`). Les cas propres à la refonte : `un_voyage_est_une_situation` 3/3,
`la_veille_voit_le_passeport` 2/2, `la_veille_se_tait_sans_raison` 2/2.

Ce que ces chiffres ne mesurent pas : la refonte change surtout ce qu'Orion fait entre les
conversations (veille, situations, tri de la mémoire), que les cas existants ne couvrent pas.
Le vrai juge sera une semaine d'usage, et `orion misses`.
