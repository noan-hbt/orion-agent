# Paquets de tools Orion

Un paquet de tool est un dossier installable contenant au minimum un
`tool.toml` et un module Python exposant `register()`. Par exemple :

```powershell
python orion_tools.py install .\tool_packages\terminal
```

Le paquet sera chargé au prochain démarrage d'Orion et exposera son tool au
modèle.

## Manifeste

Le manifeste décrit l'identité, le point d'entrée et les permissions du tool.
Il peut également fournir une section `[guidance]`, destinée à donner au
modèle le contexte opérationnel nécessaire pour utiliser correctement le tool.
Cette section est facultative : un tool qui ne la déclare pas reste valide.

```toml
id = "example.catalog"
name = "Catalogue"
version = "1.0.0"
entrypoint = "tool:register"
description = "Consulte le catalogue de produits."
api_version = 1
permissions = ["network"]

[guidance]
summary = "Recherche des produits dans le catalogue public."
instructions = "Utilise ce tool lorsque la demande concerne le catalogue. Vérifie les résultats avant de les résumer."
constraints = [
  "Ne prétends pas qu'un produit est disponible sans résultat explicite.",
  "N'utilise pas ce tool pour accéder à des données privées."
]
```

Les trois champs sont des chaînes, sauf `constraints`, qui est une liste de
chaînes :

- `summary` résume le rôle du tool en une phrase courte ;
- `instructions` décrit la procédure ou les informations importantes à suivre ;
- `constraints` énumère ses limites et précautions d'utilisation.

La guidance des tools chargés est ajoutée au contexte système de l'agent, et
elle est filtrée selon les tools effectivement disponibles (notamment pour les
sous-agents). Elle aide le modèle à choisir et employer un tool, mais ne
remplace ni son schéma d'appel, ni les permissions, ni les contrôles Orion.
Elle reste subordonnée aux instructions système et aux politiques de sécurité.

Pour cette raison, n'y placez pas de secret, de token, de donnée personnelle ou
une instruction visant à contourner une approbation. La guidance est une aide
déclarative fournie par le paquet ; elle ne confère aucune permission
supplémentaire. Elle est volontairement bornée : `summary` accepte au plus
500 caractères, `instructions` 4 000 caractères, et `constraints` au plus 20
éléments de 500 caractères chacun.
