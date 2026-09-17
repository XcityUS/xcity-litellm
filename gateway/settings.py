"""Gateway deployment mode, read from the environment.

``GATEWAY_SERVE_FULL_PROXY=true`` turns the gateway into a single-node proxy: the
route trim is skipped so the Admin UI and the management API are served next to
the LLM data plane, and pending database migrations are applied at boot. Leave
it unset for the split deployment (gateway + backend + UI containers).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from litellm.secret_managers.main import str_to_bool

FULL_PROXY_ENV_VAR: Final = "GATEWAY_SERVE_FULL_PROXY"


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    serve_full_proxy: bool

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "GatewaySettings":
        return cls(serve_full_proxy=str_to_bool(environ.get(FULL_PROXY_ENV_VAR)) is True)
