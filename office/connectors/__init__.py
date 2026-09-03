"""External data sources for the Comms desk.

Each connector exposes a single `async def fetch(since_hours) -> str`. They are
stubs until you wire real credentials; see slack.py and mail.py for what each
one needs. Returning a clear "not configured" string rather than raising keeps
the agent informed instead of crashing its task.
"""
