"""Checkpointing, and the serialiser that decides whether state survives a round trip intact.

Import from the modules directly. The two public entry points are
`persistence.checkpointer.build_checkpointer` and `persistence.serde.build_serde`; everything else
here is an implementation detail of one of those.
"""

from __future__ import annotations

__all__: list[str] = []
