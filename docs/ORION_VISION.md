# Vision d’Orion

Orion est un assistant personnel généraliste et durable. Il transforme des intentions en travail vérifiable, conserve les décisions utiles et reste disponible lorsque des équipes travaillent en arrière-plan. Développement, recherche, organisation et opérations sont des usages possibles, pas des produits distincts. L’exemple d’un lancement de SaaS illustre un parcours ; il ne limite pas Orion.

Ce document fixe une direction produit. Il ne présente pas toutes les capacités ci-dessous comme déjà disponibles. Le README et les tests décrivent les fonctions effectivement livrées.

## Une relation, des équipes adaptées

L’utilisateur conserve un interlocuteur principal. Celui-ci peut déléguer à des instances Orion spécialisées et identifier pour chaque mission son responsable, son état, ses dépendances, son résultat et ses blocages. Une instance peut servir seule ou participer à une équipe.

La hiérarchie répond à un besoin : une tâche courte reste locale ; plusieurs travaux indépendants peuvent avancer en parallèle. Ajouter un responsable doit diminuer le travail de coordination, pas multiplier les résumés et les appels aux modèles. Une délégation transmet un objectif, les contraintes nécessaires, un critère de réussite et les références utiles. Le retour contient un résultat compact, ses preuves et les décisions restantes.

## Travail durable et contrôlable

Une tâche conserve son objectif et ses étapes au-delà d’un échange ou d’un redémarrage. Les états distinguent notamment travail en attente, exécution, besoin d’information, échec et achèvement. Une erreur de transport n’équivaut jamais à une mission réussie. Les reprises doivent éviter de répéter silencieusement une action ayant des effets externes.

L’utilisateur doit pouvoir interrompre, réorienter et reprendre le travail. La propagation d’une annulation entre instances et le traitement d’une action déjà en cours doivent avoir un comportement explicite. Les tâches longues ne doivent pas rendre l’interlocuteur principal indisponible.

## Mémoire utile, inspectable et limitée

La mémoire cible distingue préférences personnelles, projets et décisions, épisodes, procédures et faits sourcés. Une entrée peut être consultée, corrigée ou supprimée et porter une provenance, une date, une confiance et une expiration. Une hypothèse reste une hypothèse.

La récupération sélectionne ce qui aide la tâche présente. Les journaux complets restent consultables sans être systématiquement ajoutés au prompt. Les secrets ne sont pas des souvenirs. Une information supprimée ne doit pas être réintroduite par une ancienne synthèse sans mécanisme explicite.

## Autonomie et environnements

Chaque instance reçoit des outils, un environnement de travail et des limites explicites. Des processus distincts ou des répertoires séparés ne constituent pas à eux seuls une isolation de sécurité. Une future exécution distante doit disposer d’une authentification, d’une autorisation et de règles de transport adaptées.

Les règles d’action doivent être appliquées au niveau de l’exécution : capacités autorisées, limites de durée et de concurrence, budgets quand mesurables, et politiques explicites pour publications, contacts, dépenses et suppressions. Une permission indiquée uniquement dans un prompt ou un manifest ne constitue pas une garantie.

## Preuves et apprentissage

Un résultat contient des éléments vérifiables adaptés au travail : tests et diff pour du code, sources pour une recherche, état final observé pour une opération. Le statut terminé n’est pas une preuve à lui seul. Les vérifications importantes doivent pouvoir être rejouées sans dépendre de l’affirmation du modèle auteur.

Les méthodes réutilisables commencent comme candidates. Leur promotion dépend de résultats observés, de régressions et de comparaisons. Orion conserve les conditions d’échec autant que les succès. Cette amélioration ne suppose pas un réentraînement permanent du modèle.

## Optimisation du fonctionnement

Les améliorations doivent porter sur des coûts ou défauts observables : taille du contexte envoyé, répétition des lectures et sérialisations, écritures inutiles, contention, attente active, nombre d’appels et temps de réponse. Les optimisations conservent les contrats et sont accompagnées de vérifications adaptées.

Une limite de texte n’est pas une mesure exacte de tokens ; un compteur local n’est pas une facture fournisseur. Les budgets monétaires ne sont présentés comme fiables que si les tarifs et consommations nécessaires sont effectivement connus. Le travail autonome s’arrête quand son objectif est satisfait, une limite atteinte ou une décision indispensable manque.

## Visibilité et attention

L’interface cible montre objectifs, responsables, tâches actives, blocages, décisions et livrables avec leurs preuves. Les budgets affichés distinguent valeurs mesurées et estimations. La proactivité regroupe les informations utiles et respecte la fréquence d’interruption choisie par l’utilisateur.

## Critères d’évolution

1. Consolider le fonctionnement actuel et disposer de tests de régression hors ligne.
2. Livrer une coordination multi-instance réellement exécutable, documentée et compatible avec Orion autonome.
3. Étendre la supervision, les limites et les reprises sur la base de scénarios vérifiés.
4. Développer mémoire structurée, environnements isolés et interface de suivi sans confondre une ébauche avec une capacité opérationnelle.

Chaque étape doit produire une capacité utilisable et décrire précisément ses limites.
