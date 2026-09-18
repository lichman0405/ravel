"""PubChem: the chemical registry, addressed by name rather than by query.

PubChem does not offer free-text relevance search. What it offers is
*resolution*: given a name, a formula, a SMILES string, or an InChIKey, it
returns the compound that identifier denotes, together with properties computed
from the deposited structure. RAVEL uses it that way and says so.

The distinction matters for how results are treated. A PubChem property record
is not a literature claim about a substance; it is the registry's own computed
value for a structure it holds. That makes it strong evidence about *identity*
— this name is this compound, with this formula and this molecular weight — and
no evidence at all about how the compound behaves in an experiment. The tier
rule classifies it as a registry; the reading is what decides what it supports.

Search therefore costs one request per candidate: PubChem's autocomplete
suggests names, and each name has to be resolved to a compound before it can be
opened. The number of candidates is capped, because a search that quietly
issues fifty requests is a search that gets the lab rate limited.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ravel.research.connectors.base import ConnectorError, HttpConnector
from ravel.research.leads import Lead

BASE = "https://pubchem.ncbi.nlm.nih.gov/rest"
#: PUG-REST, the part of the API that resolves identifiers. PubChem's
#: autocomplete endpoint sits beside it rather than under it, which is why
#: these are two constants and not one prefix.
PUG = f"{BASE}/pug"
AUTOCOMPLETE = f"{BASE}/autocomplete/compound"
COMPOUND = "https://pubchem.ncbi.nlm.nih.gov/compound"

#: The properties RAVEL records. All are computed by PubChem from the deposited
#: structure; none is inferred or converted by RAVEL.
#:
#: `SMILES` is requested by its alias rather than as `ConnectivitySMILES`
#: because PubChem answers the alias under the full name, and a name RAVEL
#: asked for but never receives would look like a compound with no SMILES.
_PROPERTIES = ",".join(
    [
        "Title",
        "MolecularFormula",
        "MolecularWeight",
        "SMILES",
        "InChIKey",
        "IUPACName",
    ]
)

#: How many autocompleted names are resolved to compounds. Each one is a
#: separate request, and this is the number RAVEL is willing to spend on a
#: single search.
MAX_RESOLVED_CANDIDATES = 5


class PubChemConnector(HttpConnector):
    """Identifier resolution and property lookup against PubChem PUG-REST."""

    name = "pubchem"

    def search(self, query: str, *, limit: int = 10) -> list[Lead]:
        """Compounds whose names match a term.

        Autocomplete first, then resolution. A name that autocompletes but
        cannot be resolved is skipped rather than emitted as a lead: a lead
        RAVEL cannot open is not a lead, and a compound page built from a
        guessed CID would open to the wrong substance.

        Raises:
            ConnectorError: PubChem was unreachable or refused.
        """
        payload = self.get_json(f"{AUTOCOMPLETE}/{quote(query)}/json", params={"limit": limit})
        terms = _autocompleted(payload)
        leads: list[Lead] = []
        for term in terms[: min(limit, MAX_RESOLVED_CANDIDATES)]:
            lead = self.lookup(term)
            if lead is not None:
                leads.append(lead)
        return leads

    def lookup(self, identifier: str) -> Lead | None:
        """The compound an identifier denotes.

        The identifier is passed to PubChem unchanged and resolved by PubChem's
        own rules, so a name, a CAS number, a SMILES string and an InChIKey all
        work and all mean whatever PubChem says they mean.

        Returns None when PubChem has no such compound.

        Raises:
            ConnectorError: PubChem was unreachable or refused.
        """
        key = identifier.strip()
        if not key:
            return None
        try:
            payload = self.get_json(
                f"{PUG}/compound/name/{quote(key, safe='')}/property/{_PROPERTIES}/JSON"
            )
        except ConnectorError as exc:
            if "no such record" in exc.detail:
                return None
            raise
        return self._lead(_properties(payload))

    def _lead(self, properties: dict[str, Any]) -> Lead | None:
        cid = properties.get("CID")
        if cid is None:
            return None
        return Lead(
            url=f"{COMPOUND}/{cid}",
            title=str(properties.get("Title") or properties.get("IUPACName") or f"CID {cid}"),
            snippet=_describe(properties),
            provider=self.name,
            identifiers={
                "pubchem_cid": str(cid),
                **(
                    {"inchikey": str(properties["InChIKey"])}
                    if properties.get("InChIKey")
                    else {}
                ),
            },
        )


def _autocompleted(payload: Any) -> list[str]:
    """The suggested terms from an autocomplete response."""
    if not isinstance(payload, dict):
        return []
    terms = payload.get("dictionary_terms")
    if not isinstance(terms, dict):
        return []
    compound = terms.get("compound")
    return [str(term) for term in compound] if isinstance(compound, list) else []


def _properties(payload: Any) -> dict[str, Any]:
    """The first property record from a PUG-REST property response."""
    if not isinstance(payload, dict):
        return {}
    table = payload.get("PropertyTable")
    rows = table.get("Properties") if isinstance(table, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return {}
    return rows[0]


def _describe(properties: dict[str, Any]) -> str:
    """A lead's snippet, assembled only from fields PubChem returned.

    Every part is labelled with where it came from, so a reader of a lead can
    see that the formula is PubChem's computed formula rather than RAVEL's
    reading of the name.
    """
    parts = []
    for label, keys in (
        ("formula", ("MolecularFormula",)),
        ("molecular weight", ("MolecularWeight",)),
        ("IUPAC name", ("IUPACName",)),
        # PubChem answers `SMILES` as `ConnectivitySMILES`; both are checked so
        # that a rename on their side yields no SMILES rather than a wrong one.
        ("SMILES", ("SMILES", "ConnectivitySMILES", "CanonicalSMILES")),
        ("InChIKey", ("InChIKey",)),
    ):
        for key in keys:
            value = properties.get(key)
            if value not in (None, ""):
                parts.append(f"{label}: {value}")
                break
    return "; ".join(parts)[:2000]
