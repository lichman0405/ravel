"""What each connector claims a record says, and what it refuses to claim.

Every one of these services is a real API returning a real payload, and every
lead RAVEL builds from one becomes a candidate for the Evidence Ledger. So the
tests are about fidelity rather than about plumbing: the abstract is the
depositor's abstract and not a summary, the type is the depositor's type and not
RAVEL's reading of the title, and a service that failed produces a `ConnectorError`
rather than a confident "no such record".

The payloads are real responses, trimmed to the fields the connectors select.
They are inline rather than in fixture files because the point of each test is
the relationship between one field and the value RAVEL derives from it, and that
is easier to check with both in front of you.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from ravel.config import Settings
from ravel.research.connectors.arxiv import API as ARXIV_API
from ravel.research.connectors.arxiv import ArxivConnector
from ravel.research.connectors.base import ConnectorError
from ravel.research.connectors.crossref import API as CROSSREF_API
from ravel.research.connectors.crossref import CrossrefConnector
from ravel.research.connectors.openalex import API as OPENALEX_API
from ravel.research.connectors.openalex import OpenAlexConnector
from ravel.research.connectors.pubchem import AUTOCOMPLETE, PUG, PubChemConnector

CROSSREF_ITEM: dict[str, Any] = {
    "DOI": "10.1021/acsaem.5c01035",
    "URL": "https://pubs.acs.org/doi/10.1021/acsaem.5c01035",
    "type": "journal-article",
    "title": ["Perovskite Film Formation Studied In Situ"],
    "container-title": ["ACS Applied Energy Materials"],
    "publisher": "American Chemical Society",
    "issued": {"date-parts": [[2025, 7]]},
    "abstract": "<jats:p>The film <jats:italic>formed</jats:italic> in 40 s.</jats:p>",
    "is-referenced-by-count": 3,
}

OPENALEX_WORK: dict[str, Any] = {
    "id": "https://openalex.org/W2963064024",
    "doi": "https://doi.org/10.1021/acsaem.5c01035",
    "title": "Perovskite Film Formation Studied In Situ",
    "publication_date": "2025-07-14",
    "type": "article",
    "abstract_inverted_index": {
        "The": [0],
        "film": [1, 4],
        "formed": [2],
        "in": [3],
        "40": [5],
        "s.": [6],
    },
}

ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1606.00335v2</id>
    <title>Hybrid Perovskite Solar Cells:
      A Review</title>
    <summary>We review the field.</summary>
    <published>2016-06-01T00:00:00Z</published>
    <arxiv:doi>10.1002/aenm.201600000</arxiv:doi>
    <arxiv:journal_ref>Adv. Energy Mater. 6 (2016)</arxiv:journal_ref>
  </entry>
</feed>
"""

PUBCHEM_AUTOCOMPLETE = {
    "dictionary_terms": {
        "compound": ["aspirin", "aspirin-d4", "aspirin aluminum", "not a real compound"]
    }
}

PUBCHEM_PROPERTIES = {
    "PropertyTable": {
        "Properties": [
            {
                "CID": 2244,
                "Title": "Aspirin",
                "MolecularFormula": "C9H8O4",
                "MolecularWeight": "180.16",
                "ConnectivitySMILES": "CC(=O)OC1=CC=CC=C1C(=O)O",
                "InChIKey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
                "IUPACName": "2-acetyloxybenzoic acid",
            }
        ]
    }
}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        research_contact_email="research@example.org",
        RAVEL_POSTGRES_DSN=None,
    )


# --------------------------------------------------------------------------
# Crossref
# --------------------------------------------------------------------------


@respx.mock
def test_a_crossref_lead_keeps_the_depositors_type_and_abstract(settings: Settings) -> None:
    respx.get(CROSSREF_API).mock(
        return_value=httpx.Response(200, json={"message": {"items": [CROSSREF_ITEM]}})
    )

    lead = CrossrefConnector(settings).search("perovskite film formation")[0]

    assert lead.title == "Perovskite Film Formation Studied In Situ"
    assert lead.identifiers["doi"] == "10.1021/acsaem.5c01035"
    # The type is what the publisher told Crossref, which is the only
    # classification richer than the domain, and is what the tier rules use.
    assert lead.identifiers["declared_type"] == "journal-article"
    assert lead.url == "https://pubs.acs.org/doi/10.1021/acsaem.5c01035"
    # Markup stripped, words untouched: an abstract that reads as prose but says
    # what the depositor wrote.
    assert lead.snippet == "The film formed in 40 s."


@respx.mock
def test_a_crossref_date_keeps_only_the_parts_that_were_published(settings: Settings) -> None:
    """Crossref omits the day when the depositor never gave one.

    Padding it would invent a precision the record does not have: `2025-07-01`
    reads as the first of July, when the record says only "July".
    """
    respx.get(CROSSREF_API).mock(
        return_value=httpx.Response(200, json={"message": {"items": [CROSSREF_ITEM]}})
    )

    assert CrossrefConnector(settings).search("x")[0].published == "2025-07"


@respx.mock
def test_a_crossref_work_with_no_abstract_has_an_empty_one(settings: Settings) -> None:
    """Not a summary of the title, and not the container's name.

    A work whose publisher deposited no abstract has no abstract, and a lead
    carrying generated prose would be indistinguishable downstream from one
    carrying the depositor's.
    """
    item = {k: v for k, v in CROSSREF_ITEM.items() if k != "abstract"}
    respx.get(CROSSREF_API).mock(
        return_value=httpx.Response(200, json={"message": {"items": [item]}})
    )

    assert CrossrefConnector(settings).search("x")[0].snippet == ""


@respx.mock
@pytest.mark.parametrize(
    "identifier",
    ["10.1021/acsaem.5c01035", "doi:10.1021/acsaem.5c01035", "https://doi.org/10.1021/x"],
)
def test_a_crossref_lookup_accepts_an_identifier_in_any_of_its_usual_forms(
    settings: Settings, identifier: str
) -> None:
    """A DOI reaches RAVEL as whatever the caller was holding."""
    route = respx.get(url__startswith=f"{CROSSREF_API}/").mock(
        return_value=httpx.Response(200, json={"message": CROSSREF_ITEM})
    )

    assert CrossrefConnector(settings).lookup(identifier) is not None
    assert route.called


@respx.mock
def test_a_doi_crossref_does_not_have_is_not_an_error(settings: Settings) -> None:
    """ "Crossref has no such DOI" is a finding about the identifier."""
    respx.get(url__startswith=f"{CROSSREF_API}/").mock(return_value=httpx.Response(404))

    assert CrossrefConnector(settings).lookup("10.9999/nonexistent") is None


@respx.mock
def test_crossref_being_down_is_an_error_and_not_an_empty_result(settings: Settings) -> None:
    """The distinction the whole module rests on.

    An unavailable registry that returns `[]` is indistinguishable from a
    registry that knows nothing, and a research task would go on to conclude
    that no such work exists.
    """
    respx.get(CROSSREF_API).mock(return_value=httpx.Response(500))

    with pytest.raises(ConnectorError) as raised:
        CrossrefConnector(settings).search("perovskite")
    assert "HTTP 500" in raised.value.detail


@respx.mock
def test_crossref_answering_with_html_is_an_error(settings: Settings) -> None:
    """A proxy's error page is not a search result."""
    respx.get(CROSSREF_API).mock(
        return_value=httpx.Response(200, text="<html>maintenance</html>")
    )

    with pytest.raises(ConnectorError, match="did not return JSON"):
        CrossrefConnector(settings).search("perovskite")


# --------------------------------------------------------------------------
# OpenAlex
# --------------------------------------------------------------------------


@respx.mock
def test_an_openalex_type_is_translated_into_the_vocabulary_the_rules_use(
    settings: Settings,
) -> None:
    """OpenAlex says `article`; Crossref says `journal-article`.

    The tier rules are written once, against one vocabulary, so the translation
    happens at the boundary rather than being duplicated in the rules.
    """
    respx.get(OPENALEX_API).mock(
        return_value=httpx.Response(200, json={"results": [OPENALEX_WORK]})
    )

    lead = OpenAlexConnector(settings).search("perovskite")[0]

    assert lead.identifiers["declared_type"] == "journal-article"
    assert lead.identifiers["openalex_id"] == "https://openalex.org/W2963064024"
    assert lead.published == "2025-07-14"


@respx.mock
def test_a_repeated_word_lands_at_every_position_it_holds(settings: Settings) -> None:
    respx.get(OPENALEX_API).mock(
        return_value=httpx.Response(200, json={"results": [OPENALEX_WORK]})
    )

    assert OpenAlexConnector(settings).search("x")[0].snippet == "The film formed in film 40 s."


@respx.mock
def test_a_gap_in_an_inverted_abstract_stays_a_gap(settings: Settings) -> None:
    """The case that decides whether a quotation can be trusted.

    Words at positions 0 and 2 with nothing at 1 reconstruct to `"The  end"`.
    Closing the gap would produce `"The end"` — a sentence the depositor never
    wrote, quoted as though they had.
    """
    work = dict(OPENALEX_WORK, abstract_inverted_index={"The": [0], "end": [2]})
    respx.get(OPENALEX_API).mock(return_value=httpx.Response(200, json={"results": [work]}))

    snippet = OpenAlexConnector(settings).search("x")[0].snippet

    assert snippet == "The  end"
    assert snippet != "The end"


@respx.mock
def test_a_work_with_no_inverted_index_has_an_empty_abstract(settings: Settings) -> None:
    work = {k: v for k, v in OPENALEX_WORK.items() if k != "abstract_inverted_index"}
    respx.get(OPENALEX_API).mock(return_value=httpx.Response(200, json={"results": [work]}))

    assert OpenAlexConnector(settings).search("x")[0].snippet == ""


@respx.mock
def test_an_openalex_lookup_falls_back_to_the_id_path_only_for_a_missing_record(
    settings: Settings,
) -> None:
    """Two addressing schemes, one of which will be the wrong one.

    `key` is tried as a DOI first and as an OpenAlex id second. Crucially, only
    a 404 moves on: a 500 on the first path means the service is unwell, and
    treating that as "try again another way" would hide it.
    """
    doi_route = respx.get(f"{OPENALEX_API}/https://doi.org/W2963064024").mock(
        return_value=httpx.Response(404)
    )
    id_route = respx.get(f"{OPENALEX_API}/W2963064024").mock(
        return_value=httpx.Response(200, json=OPENALEX_WORK)
    )

    lead = OpenAlexConnector(settings).lookup("W2963064024")

    assert lead is not None
    assert doi_route.called and id_route.called


@respx.mock
def test_an_openalex_lookup_does_not_treat_a_server_error_as_a_missing_record(
    settings: Settings,
) -> None:
    respx.get(f"{OPENALEX_API}/https://doi.org/10.1/x").mock(return_value=httpx.Response(503))

    with pytest.raises(ConnectorError):
        OpenAlexConnector(settings).lookup("10.1/x")


# --------------------------------------------------------------------------
# arXiv
# --------------------------------------------------------------------------


@respx.mock
def test_an_arxiv_lead_keeps_the_journal_reference_separate_from_the_record(
    settings: Settings,
) -> None:
    """The published version is somewhere else, and this lead is not it.

    arXiv holds the deposit. The journal reference says the work appeared
    elsewhere; folding it in would make a preprint lead look like the version of
    record and hide that the published article is a different source.
    """
    respx.get(ARXIV_API).mock(return_value=httpx.Response(200, text=ARXIV_FEED))

    lead = ArxivConnector(settings).search("perovskite")[0]

    assert lead.identifiers["declared_type"] == "posted-content"
    assert lead.identifiers["journal_ref"] == "Adv. Energy Mater. 6 (2016)"
    assert lead.identifiers["doi"] == "10.1002/aenm.201600000"
    assert lead.identifiers["arxiv_id"] == "1606.00335v2"


@respx.mock
def test_an_arxiv_url_is_recorded_as_https(settings: Settings) -> None:
    """The scheme arXiv writes is not the scheme RAVEL reads.

    The feed says `http://`. Storing that would put a URL in the ledger that
    RAVEL never fetched and would not choose to.
    """
    respx.get(ARXIV_API).mock(return_value=httpx.Response(200, text=ARXIV_FEED))

    assert ArxivConnector(settings).search("x")[0].url == "https://arxiv.org/abs/1606.00335v2"


@respx.mock
def test_an_arxiv_title_is_joined_across_its_line_breaks(settings: Settings) -> None:
    """Atom wraps titles with newlines that are not part of the title."""
    respx.get(ARXIV_API).mock(return_value=httpx.Response(200, text=ARXIV_FEED))

    assert ArxivConnector(settings).search("x")[0].title == (
        "Hybrid Perovskite Solar Cells: A Review"
    )


def test_arxiv_entities_in_a_response_are_not_expanded(settings: Settings) -> None:
    """The parser is configured for a document written by a stranger.

    A ten-line response whose entity expands to a gigabyte is the classic way to
    take down a client that parses XML from the network. The entity is left as
    text; nothing is fetched, and nothing is expanded.
    """
    bomb = """<?xml version="1.0"?>
    <!DOCTYPE feed [
      <!ENTITY a "aaaaaaaaaa">
      <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
      <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
    ]>
    <feed xmlns="http://www.w3.org/2005/Atom"><entry><title>&c;</title></entry></feed>
    """

    leads = ArxivConnector(settings)._parse(bomb)

    assert len(leads) == 1
    assert len(leads[0].title) < 100


def test_a_response_that_is_not_xml_is_a_connector_error(settings: Settings) -> None:
    with pytest.raises(ConnectorError, match="not Atom XML"):
        ArxivConnector(settings)._parse("<html>we are down for maintenance")


def test_arxiv_waits_between_requests(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """arXiv asks for one request every three seconds, so RAVEL takes three.

    Free services that get hammered start blocking the lab's address, and the
    cost of compliance is a few seconds. The clock is replaced rather than
    waited on: a test that sleeps three seconds to prove a three-second sleep is
    a test that slows down every run to check nothing extra.
    """
    sleeps: list[float] = []
    clock = [1000.0]

    class FakeTime:
        @staticmethod
        def monotonic() -> float:
            return clock[0]

        @staticmethod
        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds

    monkeypatch.setattr("ravel.research.connectors.arxiv.time", FakeTime)
    connector = ArxivConnector(settings)
    connector._wait()  # the first request pays nothing
    assert sleeps == []

    clock[0] += 1.0
    connector._wait()
    assert sleeps == [2.0]


# --------------------------------------------------------------------------
# PubChem
# --------------------------------------------------------------------------


@respx.mock
def test_a_pubchem_search_resolves_each_suggested_name_to_a_compound(
    settings: Settings,
) -> None:
    """Autocomplete suggests strings; a compound is a CID.

    A lead built from a suggested name that was never resolved would point at
    whatever PubChem decided the string meant, if it meant anything at all.
    """
    respx.get(f"{AUTOCOMPLETE}/aspirin/json").mock(
        return_value=httpx.Response(200, json=PUBCHEM_AUTOCOMPLETE)
    )
    respx.get(url__startswith=f"{PUG}/compound/name/").mock(
        return_value=httpx.Response(200, json=PUBCHEM_PROPERTIES)
    )

    leads = PubChemConnector(settings).search("aspirin")

    assert leads
    assert leads[0].url == "https://pubchem.ncbi.nlm.nih.gov/compound/2244"
    assert leads[0].identifiers["pubchem_cid"] == "2244"


@respx.mock
def test_a_name_that_cannot_be_resolved_is_dropped_and_not_emitted(
    settings: Settings,
) -> None:
    """One suggestion is real, the other is not, and only one becomes a lead."""
    respx.get(f"{AUTOCOMPLETE}/aspirin/json").mock(
        return_value=httpx.Response(
            200, json={"dictionary_terms": {"compound": ["aspirin", "not a real compound"]}}
        )
    )
    respx.get(url__startswith=f"{PUG}/compound/name/aspirin/").mock(
        return_value=httpx.Response(200, json=PUBCHEM_PROPERTIES)
    )
    respx.get(url__startswith=f"{PUG}/compound/name/not%20a%20real%20compound/").mock(
        return_value=httpx.Response(404)
    )

    leads = PubChemConnector(settings).search("aspirin")

    assert [lead.identifiers["pubchem_cid"] for lead in leads] == ["2244"]


@respx.mock
def test_a_search_resolves_no_more_candidates_than_it_says_it_will(
    settings: Settings,
) -> None:
    """A search that quietly issues fifty requests gets the lab rate limited."""
    from ravel.research.connectors.pubchem import MAX_RESOLVED_CANDIDATES

    names = [f"compound {index}" for index in range(20)]
    respx.get(f"{AUTOCOMPLETE}/x/json").mock(
        return_value=httpx.Response(200, json={"dictionary_terms": {"compound": names}})
    )
    route = respx.get(url__startswith=f"{PUG}/compound/name/").mock(
        return_value=httpx.Response(200, json=PUBCHEM_PROPERTIES)
    )

    PubChemConnector(settings).search("x")

    assert route.call_count == MAX_RESOLVED_CANDIDATES


@respx.mock
def test_a_pubchem_snippet_labels_where_each_value_came_from(settings: Settings) -> None:
    """The formula is PubChem's computed formula, not RAVEL's reading of a name.

    Labelling each part is what lets a later reader tell a registry's computed
    value from an author's claim.
    """
    respx.get(url__startswith=f"{PUG}/compound/name/").mock(
        return_value=httpx.Response(200, json=PUBCHEM_PROPERTIES)
    )

    snippet = PubChemConnector(settings).lookup("aspirin").snippet  # type: ignore[union-attr]

    assert "formula: C9H8O4" in snippet
    assert "molecular weight: 180.16" in snippet
    assert "SMILES: CC(=O)OC1=CC=CC=C1C(=O)O" in snippet
    assert "InChIKey: BSYNRYMUTXBXSQ-UHFFFAOYSA-N" in snippet


@respx.mock
def test_a_pubchem_compound_with_no_cid_is_not_a_lead(settings: Settings) -> None:
    """A property table without a CID identifies nothing to open."""
    respx.get(url__startswith=f"{PUG}/compound/name/").mock(
        return_value=httpx.Response(200, json={"PropertyTable": {"Properties": [{"Title": "x"}]}})
    )

    assert PubChemConnector(settings).lookup("x") is None


@respx.mock
def test_a_compound_pubchem_does_not_have_is_not_an_error(settings: Settings) -> None:
    respx.get(url__startswith=f"{PUG}/compound/name/").mock(return_value=httpx.Response(404))

    assert PubChemConnector(settings).lookup("unobtainium") is None


@respx.mock
def test_every_connector_identifies_itself_to_every_service(settings: Settings) -> None:
    """One setting, applied everywhere, on the path that actually runs.

    Crossref, OpenAlex and NCBI all ask for a contact address and give
    identified clients a better pool; the assertion is on the wire rather than
    on the attribute, because the attribute is not what the service sees.
    """
    respx.get(OPENALEX_API).mock(return_value=httpx.Response(200, json={"results": []}))

    OpenAlexConnector(settings).search("x")

    agent = respx.calls[0].request.headers["user-agent"]
    assert agent.startswith("RAVEL/")
    assert "research@example.org" in agent
    assert respx.calls[0].request.url.params["mailto"] == "research@example.org"


def test_a_connector_without_a_contact_address_cannot_make_a_request(
    settings: Settings,
) -> None:
    """Constructible, so one missing setting does not disarm every connector.

    It still cannot fetch: the request goes out with an agent naming no contact,
    which is exactly the anonymous client these services throttle.
    """
    anonymous = Settings(env="test", research_contact_email=None, RAVEL_POSTGRES_DSN=None)
    connector = CrossrefConnector(anonymous)

    assert "mailto" not in connector.user_agent
    assert connector.contact == ""


def test_a_payload_that_is_not_the_expected_shape_yields_no_leads(settings: Settings) -> None:
    """Every parser is total over malformed input.

    These run on responses from third parties, and a shape change must produce
    no leads rather than an exception in the middle of a research task.
    """
    assert CrossrefConnector._items({"message": "not a dict"}) == []
    assert CrossrefConnector._items(None) == []
    # An OpenAlex work with neither a DOI nor an id still has to name something
    # openable, so it falls back to the endpoint rather than to an empty string.
    assert OpenAlexConnector(settings)._lead({}).url == OPENALEX_API


def test_json_is_read_from_the_body_and_not_assumed(settings: Settings) -> None:
    """`get_json` reports the URL when a service answers with something else."""
    with respx.mock:
        respx.get(CROSSREF_API).mock(
            return_value=httpx.Response(200, text=json.dumps(["a list, not a message"]))
        )
        leads = CrossrefConnector(settings).search("x")

    assert leads == []
