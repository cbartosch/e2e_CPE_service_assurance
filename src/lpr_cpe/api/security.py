"""Who may call a write endpoint, and the one profile in which the question has an answer.

The specification asks to "authenticate write endpoints in the production profile", and that
qualifier is the whole design. In simulation every adapter is a fixture and every write is recorded
rather than sent, so a token would protect nothing and would mostly serve to make the demonstration
harder to run. In production a write reaches a real system, and an unauthenticated resume endpoint
is
a way for anyone who can reach the port to approve a truck roll.

So the check is conditional, and the failure mode it is written against is the one where it is
*skipped* rather than the one where it compares wrongly. Two things follow:

* `require_write_token` resolves the settings on every call rather than closing over them at import.
  A process whose profile changed under it -- which is what a config reload is -- must not keep
  answering from the profile it booted with.
* The absence of a configured secret in production is a **refusal**, not a bypass. A deployment that
  turned on production mode and forgot the secret is the exact case where "no secret means no check"
  would be worst, so it fails closed with a 500 that names the setting. `policies.engine` fails
  closed for the same reason and says so at greater length.

The token is compared with `secrets.compare_digest`, which is not superstition: a plain `==` on a
str leaks the length of the matching prefix through timing, and this is the one comparison in the
system where an attacker chooses one side of it.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from lpr_cpe.config.settings import AppMode, Settings, get_settings


def _resolved(request: Request) -> Settings:
    """The settings *this app* runs under, not the process-wide ones.

    Reads `app.state.settings`, which the lifespan sets from whatever `build_app` was given, and
    falls back to `get_settings()` for an app built without one. Getting this wrong is not
    theoretical: the first version called `get_settings()` unconditionally, so
    `build_app(settings=production)` produced an app whose *graph* ran in the production profile and
    whose *guard* read the ambient simulation settings -- every write endpoint open, and the test
    that was supposed to prove otherwise failing with `assert 202 == 401`. An injectable dependency
    that ignores the injection is worse than no injection.
    """
    from_app = getattr(request.app.state, "settings", None)
    return from_app if isinstance(from_app, Settings) else get_settings()


def require_write_token(
    settings: Annotated[Settings, Depends(_resolved)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Guard a write endpoint. A no-op outside the production profile; a refusal inside it.

    Raises `503` rather than `401` when production is configured with no secret, because the caller
    did nothing wrong and retrying with a different token cannot help. That distinction matters to
    whoever is holding the pager: a 401 says "your credential is wrong" and a 503 says "this
    deployment is misconfigured", and conflating them sends them to the wrong place.
    """
    if settings.app_mode is not AppMode.PRODUCTION:
        return

    expected = settings.webhook_secret
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "this deployment runs in the production profile with no LPR_WEBHOOK_SECRET set, so "
                "write endpoints cannot be authenticated and are refused. Set the secret or run in "
                "the simulation profile."
            ),
        )

    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[len("bearer ") :].strip()

    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="a write endpoint in the production profile requires `Authorization: Bearer …`",
            headers={"WWW-Authenticate": "Bearer"},
        )


WriteGuard = Annotated[None, Depends(require_write_token)]

__all__ = ["WriteGuard", "require_write_token"]
