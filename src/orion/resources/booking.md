# Prendre rendez-vous

« Trouve-moi un dentiste cette semaine après 18 h », « réserve-nous une table jeudi », « prends
le train de 8 h pour Lyon » : tu le fais de bout en bout.

1. Cherche les créneaux toi-même (`browser` sur Doctolib, TheFork, SNCF Connect : ils se
   voient sans compte), en tenant compte de son agenda (`agenda`). Propose-lui au plus trois
   créneaux, en une ligne chacun : quand, où, avec qui, le prix s'il y en a un.
2. Quand il en choisit un, dans le même tour : `booking` (book) avec le créneau exact, puis
   réserve-le toi-même dans ton navigateur (`browser` login s'il faut son compte, puis
   `browser` send sur la page de réservation). Arrête-toi avant tout paiement, sauf s'il l'a
   demandé. Il approuve l'envoi, et ce oui vaut pour l'agenda.
3. Dès que le site a répondu : `booking` (done), réussie ou non. Réussie, le rendez-vous va dans
   son agenda avec un rappel la veille et avant l'heure : dis-le-lui en une phrase. Échouée,
   dis-le-lui et propose un autre créneau.

Pour annuler : `booking` (cancel), puis annule-la sur le site avec `browser` send.
