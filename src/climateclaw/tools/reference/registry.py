from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Reference:
    id: str
    description: str
    url: str
    mime_type: str


REFERENCES: dict[str, Reference] = {
    "mkexp-manual": Reference(
        id="mkexp-manual",
        description=(
            "Official mkexp manual for creating and configuring experiments. "
            "Covers experiment configuration, run_start, namelists, "
            "reinitialization, cpexp, diffexp, runscripts, and related topics."
            "Also consult the ICON documentation using the web_search tool because "
            "relevant configuration context may be split between the mkexp manual "
            "and ICON docs."
        ),
        url=("https://gitlab.dkrz.de/esmenv/mkexp/-/raw/master/doc/mkexp.pdf"),
        mime_type="application/pdf",
    ),
}


def get_reference(reference_id: str) -> Reference:
    try:
        return REFERENCES[reference_id]
    except KeyError as exc:
        available = ", ".join(sorted(REFERENCES))
        raise ValueError(
            f"Unknown reference {reference_id!r}. Available references: {available}"
        ) from exc


def reference_catalog() -> str:
    return "\n".join(f"- {ref.id}: {ref.description}" for ref in REFERENCES.values())
