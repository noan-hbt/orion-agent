# Paquets de tools Orion

Le dossier `echo/` est un exemple de paquet installable :

```powershell
python orion_tools.py install .\tool_packages\echo
```

Il sera ensuite chargé au prochain démarrage d'Orion et exposera le tool
`echo` au modèle.
