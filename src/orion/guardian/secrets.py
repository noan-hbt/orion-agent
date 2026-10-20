"""Which secrets belong to the guardian process when Orion runs split (D28)."""

from __future__ import annotations

import os
from collections.abc import Mapping

from orion.config import OrionConfig, SecretRef


def guarded(config: OrionConfig) -> list[SecretRef]:
    """Every secret that reaches the outside world: only the guardian may hold them. The core
    keeps the LLM key alone."""
    from orion.calendar.service import CalendarSettings
    from orion.mail.service import MailSettings

    refs = [
        config.channels.imessage.api_key,
        config.channels.imessage.signing_key,
        config.federation.key,
    ]
    if "calendar" in config.modules:
        from orion.calendar.write import CalDavWriter, GoogleWriter

        for account in CalendarSettings.model_validate(config.modules["calendar"]).accounts:
            refs.append(account.url)
            if isinstance(account.write, CalDavWriter):
                refs.append(account.write.password)
            elif isinstance(account.write, GoogleWriter):
                refs += [account.write.client_secret, account.write.refresh_token]
    if "mail" in config.modules:
        refs += [a.password for a in MailSettings.model_validate(config.modules["mail"]).accounts]
    return refs


def exposed(config: OrionConfig, environ: Mapping[str, str] | None = None) -> list[str]:
    """Guarded secrets visible in this process's environment."""
    env = os.environ if environ is None else environ
    return sorted({ref.env for ref in guarded(config) if env.get(ref.env)})
