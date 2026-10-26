import pytest

from gateway.settings import FULL_PROXY_ENV_VAR, GatewaySettings


@pytest.mark.parametrize("value", ["true", "True", " TRUE "])
def test_full_proxy_mode_is_opt_in_by_env(value: str):
    assert GatewaySettings.from_env({FULL_PROXY_ENV_VAR: value}).serve_full_proxy is True


@pytest.mark.parametrize(
    "environ", [{}, {FULL_PROXY_ENV_VAR: "false"}, {FULL_PROXY_ENV_VAR: "yes"}, {FULL_PROXY_ENV_VAR: ""}]
)
def test_anything_but_true_keeps_the_trimmed_gateway(environ: dict[str, str]):
    assert GatewaySettings.from_env(environ).serve_full_proxy is False
