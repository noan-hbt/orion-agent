"""Tool d'exemple installable avec ``python orion_tools.py install``."""

from __future__ import annotations

from typing import Any


def echo(text: str) -> dict[str, Any]:
    return {"text": text}


def register(client: Any, context: Any = None) -> None:
    client.register_tool(
        "echo",
        echo,
        description="Renvoyer exactement un texte fourni par l'utilisateur.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )
