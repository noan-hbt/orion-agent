# Prendre rendez-vous

« Trouve-moi un dentiste cette semaine après 18 h », « réserve-nous une table jeudi », « prends
le train de 8 h pour Lyon » : tu le fais de bout en bout.

1. Cherche les créneaux toi-même (`browser` sur Doctolib, TheFork, SNCF Connect : ils se
   voient sans compte), en tenant compte de son agenda (`agenda`). Propose-lui au plus trois
   créneaux, en une ligne chacun : quand, où, avec qui, le prix s'il y en a un.
2. Quand il en choisit un, dans le même tour : `booking` (book) avec le créneau exact, puis
   `computer_task_send` qui réserve ce créneau-là (sur `sites`, avec sa `session` s'il faut
   être connecté). La consigne dit précisément quoi réserver, avec quelles informations, et de
   s'arrêter avant tout paiement. Il approuve la consigne, et ce oui vaut pour l'agenda.
3. La suite se fait seule : quand la réservation a réussi, le rendez-vous va dans son agenda
   avec un rappel la veille et avant l'heure, et tu le lui dis en une phrase. Si elle échoue,
   tu le lui dis et proposes un autre créneau.

Pour annuler : `booking` (cancel), puis `computer_task_send` pour l'annuler sur le site.
