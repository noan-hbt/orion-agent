# Orion installable tool packages

## Package layout

The current package format is deliberately small:

```text
my-tool/
├── tool.toml
└── tool.py
```

`tool.toml`:

```toml
id = "provider.lookup"
name = "Provider Lookup"
version = "1.0.0"
description = "Interroge le service Provider."
entrypoint = "tool:register"
api_version = 1
permissions = ["network"]
```

The identifier may contain letters, digits, dots, hyphens and underscores. The
entrypoint uses `module:function` syntax. `register` may accept either one
argument (`client`) or two (`client, context`). The context contains:

```python
context.root_dir
context.data_dir
context.install_dir
context.config
```

Example:

```python
def lookup(query: str) -> dict:
    return {"query": query, "items": []}


def register(client, context=None):
    client.register_tool(
        "provider_lookup",
        lookup,
        description="Rechercher des informations chez Provider.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        side_effect=False,
    )
```

## Lifecycle

The user installs a directory, zip, or zip URL:

```powershell
python orion_tools.py install .\my-tool
python orion_tools.py list
```

At the next Orion startup, `OrionConfig.build()` creates `ToolManager`, which
discovers enabled packages under `[tools].directory` and calls their
`register` function on the OpenRouter client. The model then receives the
tool's JSON definition. Updating uses the source recorded in
`data/installed_tools.json`:

```powershell
python orion_tools.py update provider.lookup
python orion_tools.py update --all
```

The current implementation does not yet provide a central catalog, signature
verification, isolated virtual environments, or automatic dependency
installation. Treat URL and archive sources as trusted executable code.
