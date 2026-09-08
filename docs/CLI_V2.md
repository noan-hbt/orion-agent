# CLI v2

`cli_v2.py` is a new terminal adapter, independent of `cli_ui.py` and of
optional UI libraries. It implements the `ChannelAdapter` contract:

```python
from cli_v2 import OrionCLIAdapter, run

cli = OrionCLIAdapter()
application.channels.register(cli)  # name is ``cli``
application.start()
cli.loop()
application.stop()
```

The adapter accepts `terminal`, `plain`, `json`, and `jsonl` output modes.
`submit()` creates an `InboundMessage`; `send()` appends an immutable
`TranscriptEvent`. Provider setters (`set_status_provider`,
`set_tools_provider`, etc.) are optional and are exposed by the runtime
configuration. The existing `orion_run.run_once` remains a separate,
non-interactive entry point and is not imported by this module.
