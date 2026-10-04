import inspect
import re
from pathlib import Path
from typing import Final


def extract_documented_router_settings(content: str) -> frozenset[str]:
    section: Final = re.search(
        r"^### router_settings - Reference[ \t]*\n(.*?)(?=^#{1,3}[ \t]|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    if section is None:
        return frozenset()
    return frozenset(
        re.findall(r"^[ \t]*\|[ \t]*`?([a-z][a-z0-9_]*)`?[ \t]*\|", section.group(1), re.MULTILINE)
    )


def main() -> None:
    import litellm

    router_init_params: Final = frozenset(inspect.signature(litellm.Router).parameters) - {"model_list"}
    repo_root: Final = Path(__file__).resolve().parents[2]
    docs_path: Final = repo_root / "docs" / "my-website" / "docs" / "proxy" / "config_settings.md"
    documented_keys: Final = extract_documented_router_settings(docs_path.read_text(encoding="utf-8"))
    undocumented_keys: Final = router_init_params - documented_keys
    if undocumented_keys:
        raise ValueError(f"Keys not documented in 'router settings - Reference': {sorted(undocumented_keys)}")
    print("All Router settings are documented in 'router settings - Reference'")


if __name__ == "__main__":
    main()
