"""Build the package description from README.md without its lifecycle block.

The README's Production lifecycle block is regenerated from the live signed
record and checked against it every day. PyPI keeps a release's description
forever, so a copied block would keep stating an admission state after the
record changes. The 1.16.0 page still says "Not actively admitted" while the
README names active admissions. The package description replaces the block
with a pointer to the live record. `scripts/verify_release_artifacts.py`
refuses a wheel or source archive whose description still states a lifecycle
state.
"""

from __future__ import annotations

from pathlib import Path

BEGIN = "<!-- BEGIN PRODUCTION LIFECYCLE -->"
END = "<!-- END PRODUCTION LIFECYCLE -->"
LIVE_RECORD_URL = "https://docs.openadapt.ai/production-lifecycle.json"
RELEASE_POINTER = (
    "PyPI shows the README as it was at release and never updates it, so this "
    "copy leaves the status out.\n"
    f"[Check the live signed Production record]({LIVE_RECORD_URL}) for the "
    "current status."
)


def package_description(readme: str) -> str:
    """Return the README with its lifecycle block replaced by the pointer."""
    if BEGIN not in readme and END not in readme:
        return readme
    before, _, rest = readme.partition(BEGIN)
    if readme.count(BEGIN) != 1 or readme.count(END) != 1 or END not in rest:
        raise ValueError("README must contain one complete lifecycle block")
    _, _, after = rest.partition(END)
    return before + RELEASE_POINTER + after


try:
    from hatchling.metadata.plugin.interface import MetadataHookInterface
except ImportError:  # The test suite loads this file without hatchling.
    MetadataHookInterface = None

if MetadataHookInterface is not None:

    class PackageDescriptionHook(MetadataHookInterface):
        """Set the dynamic `readme` field from README.md."""

        def update(self, metadata: dict) -> None:
            readme = Path(self.root, "README.md").read_text(encoding="utf-8")
            metadata["readme"] = {
                "content-type": "text/markdown",
                "text": package_description(readme),
            }
