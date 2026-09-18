"""How a source is rated, and whether the rating can be argued with.

The tier is the one judgement RAVEL makes about a source before reading it, and
it is the judgement a later decision leans on. Two properties make it usable
rather than merely present: every assignment names the rule that produced it,
and the rules are ordered so that the answer does not depend on which rule
happened to be checked first.

The tests are mostly about the boundaries, because that is where a
classification scheme goes wrong: which rule wins when two match, what happens
to a domain nobody claimed, and what a record's own declared type does to the
domain's answer.
"""

from __future__ import annotations

import pytest

from ravel.domain.enums import EvidenceSourceTier as Tier
from ravel.research import tiers


def test_a_preprint_server_is_not_rated_as_a_publisher() -> None:
    """Ordering, tested through a pair of rules that both could match.

    `arxiv.org` is a repository and `nature.com` is a publisher, and neither
    host is under the other's suffix — so this asserts the simple case, and the
    overlapping cases are below.
    """
    assert tiers.assign("https://arxiv.org/abs/1606.00335").tier is Tier.B
    assert tiers.assign("https://www.nature.com/articles/x").tier is Tier.A


def test_the_specific_rule_wins_over_the_general_one() -> None:
    """`pubs.acs.org` is a publisher, not merely an academic-looking host.

    The publisher rule is stated before the institution rule precisely so that
    this is decided by the list's order rather than by which suffix is longer.
    """
    assignment = tiers.assign("https://pubs.acs.org/doi/10.1021/x")
    assert assignment.rule == "publisher"
    assert assignment.tier is Tier.A


def test_a_domain_no_rule_claims_is_rated_lowest() -> None:
    """The default has to be the weakest defensible claim.

    A rule that promoted the unknown would make it profitable to be unknown:
    an unattributable blog would outrank a preprint.
    """
    assignment = tiers.assign("https://someblog.example.com/a-post")
    assert assignment.tier is Tier.D
    assert assignment.rule == "unclassified"
    assert "someblog.example.com" in assignment.detail


def test_a_bare_top_level_suffix_matches_a_domain_but_not_a_host_that_merely_ends_in_it() -> None:
    """`gov` is a suffix, not a substring.

    `weather.gov` is a government body. `gov.example.com` is not, and a rule
    that matched it would let anyone claim a tier by choosing a hostname.
    """
    assert tiers.assign("https://www.weather.gov/something").tier is Tier.B
    assert tiers.assign("https://gov.example.com/x").tier is Tier.D
    # And a host the registry rule names explicitly keeps its higher rating,
    # even though it also ends in `gov`: NIST publishes the standard itself.
    assert tiers.assign("https://www.nist.gov/x").rule == "registry"


def test_a_declared_type_outranks_the_domain() -> None:
    """What the record says about the work beats what the host says about the site.

    A posted preprint on a publisher's own domain is still a preprint, and the
    assignment has to say which source of truth it used.
    """
    assignment = tiers.assign("https://www.nature.com/articles/x", declared_type="posted-content")
    assert assignment.tier is Tier.B
    assert assignment.rule == "crossref:posted-content"


def test_supporting_information_does_not_inherit_the_articles_standing() -> None:
    """The case that made the rule necessary.

    ACS registers supporting information as a separate DOI, and doi.org
    redirects it to the same publisher as the article it accompanies. Rated by
    domain alone it would be Tier A — the same as a peer-reviewed paper — which
    overstates a file of spectra.
    """
    supporting = tiers.assign(
        "https://pubs.acs.org/doi/suppl/10.1021/x", declared_type="component"
    )
    article = tiers.assign("https://pubs.acs.org/doi/10.1021/x", declared_type="journal-article")
    assert supporting.tier is Tier.B
    assert supporting.tier.value > article.tier.value


def test_an_unrecognized_declared_type_is_not_promoted_by_its_domain() -> None:
    """A type RAVEL cannot classify falls back to a low rating, not to the host's.

    Falling through to the domain rule would rate a record the depositor
    declined to call an article by the reputation of the site it resolves
    through. The answer names the type, so the gap is visible rather than
    silently absorbed.
    """
    assignment = tiers.assign("https://www.nature.com/articles/x", declared_type="erratum")
    assert assignment.tier is Tier.C
    assert assignment.rule == "unrecognized-declared-type"
    assert "erratum" in assignment.detail


def test_a_container_record_is_not_a_finding() -> None:
    """A DOI for a journal is a table of contents, not a result."""
    assignment = tiers.assign("https://doi.org/10.1002/x", declared_type="journal")
    assert assignment.tier is Tier.C
    assert assignment.rule == "crossref:container"


def test_a_grant_is_not_a_result() -> None:
    """A funding record says work was paid for, not what it found."""
    assignment = tiers.assign("https://doi.org/10.1002/x", declared_type="grant")
    assert assignment.tier is Tier.C


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/path",
        "http://example.com",
        "https://user:password@example.com/path",
        "https://example.com:8443/path",
        "https://EXAMPLE.COM/Path",
        "https://www.example.com/path",
        "not a url at all",
    ],
)
def test_every_assignment_is_total_and_carries_a_reason(url: str) -> None:
    """No input produces an exception or a reasonless tier.

    This runs on URLs assembled by a model from a search result, so it meets
    malformed input routinely. An unrecognized host is a fact about RAVEL's
    rules, not a reason to fail a research task.
    """
    assignment = tiers.assign(url)
    assert assignment.tier in tuple(Tier)
    assert assignment.rule
    assert assignment.detail


def test_the_host_is_read_from_the_url_and_not_from_the_path() -> None:
    """A URL that mentions `nature.com` in its path is not hosted by Nature."""
    assignment = tiers.assign("https://example.com/redirect?to=https://www.nature.com/x")
    assert assignment.tier is Tier.D
    assert tiers.host_of("https://www.ars.usda.gov/x") == "ars.usda.gov"


def test_every_rule_is_reachable() -> None:
    """Each rule fires for at least one of its own suffixes.

    A rule whose suffix can never match is dead code that reads like coverage.
    """
    for rule in tiers.RULES:
        for suffix in rule.suffixes:
            assignment = tiers.assign(f"https://www.{suffix}/x")
            assert assignment.rule == rule.name, (
                f"{suffix!r} is listed under {rule.name!r} but matched {assignment.rule!r}"
            )


def test_a_domain_appears_under_exactly_one_rule() -> None:
    """Two rules claiming one suffix would make the answer depend on list order.

    Order is meaningful here — the strongest rule is stated first — but a
    duplicate would mean an entry silently never fires.
    """
    seen: dict[str, str] = {}
    for rule in tiers.RULES:
        for suffix in rule.suffixes:
            assert suffix not in seen, (
                f"{suffix!r} is listed under both {seen.get(suffix)!r} and {rule.name!r}; "
                "only the earlier one can ever fire"
            )
            seen[suffix] = rule.name


def test_the_counts_report_the_strongest_tier_present() -> None:
    counts = tiers.TierCounts(counts={Tier.C: 2, Tier.B: 1})
    assert counts.total == 3
    assert counts.strongest is Tier.B


def test_counts_of_nothing_have_no_strongest_tier() -> None:
    """An empty body of evidence is not strong, and has no strongest member."""
    counts = tiers.TierCounts()
    assert counts.total == 0
    assert counts.strongest is None
