"""Real, redistributable RASPA inputs, pinned by commit and by content.

The first supported scientific route is not allowed to rest on invented data.
Every structure, force-field entry, and molecule definition this package copies
is a file from the official RASPA2 repository at a pinned commit, and every
one is named here with its SHA-256. The bundle lives beside this module, but
the digests are the identity: if the bytes on disk stop hashing to these
values, the package refuses to build rather than preparing a calculation whose
force field is not the one the record cites.

**Why the whole file rather than a citation.** A citation names a paper; a run
depends on the exact numbers transcribed into a `.def` file. Pinning the file's
digest is what makes "the same inputs" a checkable claim instead of a
bibliographic one.

**Licensing.** RASPA2 is MIT-licensed (``COPYING``, pinned below), and the
bundled files — the MFI structure, the ExampleZeolitesForceField data, and the
ExampleDefinitions methane definition — are distributed in that same
repository under that same license. The citation fields are attribution for
the science, not a second license.

**The reference output is not a RAVEL run.** `official_output_100000` is an
unmodified output the RASPA authors committed for the official example. It is
shipped with the T10 parser tests as a reference fixture and is labelled
`REFERENCE_FIXTURE` wherever it is parsed. It proves the parser reads RASPA's
format; it does not prove RAVEL ran anything, and it must never be registered
as a `COMPUTATION`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

__all__ = [
    "BUNDLED_DATA_ROOT",
    "OFFICIAL_COMMIT",
    "OFFICIAL_REPOSITORY",
    "OFFICIAL_TAG",
    "REFERENCE_OUTPUT",
    "SOURCE_KEYS",
    "SOURCE_RECORDS",
    "BundledDataError",
    "SourceKind",
    "SourceRecord",
    "official_source_document",
    "read_bundled",
    "source_record",
]

OFFICIAL_REPOSITORY = "https://github.com/iRASPA/RASPA2"
OFFICIAL_TAG = "v2.0.47"
OFFICIAL_COMMIT = "673ad2d52a9c11e5ee7db9e5e7339663bfabecf9"
OFFICIAL_LICENSE = "MIT"

#: Where the bundle is, relative to this module. It mirrors the layout of a
#: RASPA installation's ``share/raspa`` directory so that the same files can be
#: read from a host installation when a deployment has one.
BUNDLED_DATA_ROOT = Path(__file__).resolve().parent / "data"


class BundledDataError(Exception):
    """The bundled reference data is absent or is not the pinned bytes.

    The class is separate from a contract refusal because this is not the
    contract's fault: the deployment's copy of RAVEL is incomplete or has been
    edited. Both cases are answered by repairing the installation, which is a
    different next step for Master than revising a contract.
    """

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One official file, as the repository holds it.

    ``key`` is the name the manifest and the ledger trace use. ``path`` is the
    path inside the official repository at ``OFFICIAL_COMMIT``; the URL in the
    manifest is built from the three, never typed twice.
    """

    key: str
    title: str
    path: str
    sha256: str
    bundled_path: str
    license: str = OFFICIAL_LICENSE
    citation: str = ""
    license_ref: str = "COPYING"
    role: str = "reference"

    @property
    def url(self) -> str:
        return f"{OFFICIAL_REPOSITORY}/blob/{OFFICIAL_COMMIT}/{self.path}"

    @property
    def raw_url(self) -> str:
        return f"{OFFICIAL_REPOSITORY}/raw/{OFFICIAL_COMMIT}/{self.path}"

    def document(self) -> dict[str, str]:
        """The record as the package manifest carries it."""
        return {
            "key": self.key,
            "title": self.title,
            "url": self.url,
            "raw_url": self.raw_url,
            "repository": OFFICIAL_REPOSITORY,
            "commit": OFFICIAL_COMMIT,
            "tag": OFFICIAL_TAG,
            "path": self.path,
            "sha256": self.sha256,
            "license": self.license,
            "license_ref": self.license_ref,
            "citation": self.citation,
            "role": self.role,
        }


_LICENSE = SourceRecord(
    key="raspa2-license",
    title="RASPA2 COPYING (MIT License)",
    path="COPYING",
    sha256="aebcbd8e5a88df1a63cccab064a3a134aebaa839181ee657426b94ea8c10441b",
    bundled_path="COPYING",
    citation=(
        "RASPA: a molecular-dynamics, monte-carlo and optimization code for "
        "nanoporous materials. Copyright (C) 2006-2019 David Dubbeldam, Sofia "
        "Calero, Thijs Vlugt, Donald E. Ellis, and Randall Q. Snurr."
    ),
)

_EXAMPLE_INPUT = SourceRecord(
    key="official-simulation-input",
    title="Official example: Adsorption of methane in MFI (simulation.input)",
    path="examples/Basic/7_Adsorption_of_Methane_in_MFI/simulation.input",
    sha256="3858806f3835f117f01ebd308b22a465276ca4e5368be3eff201bbb840acee13",
    bundled_path="simulation.input",
    citation=(
        "RASPA2 examples/Basic/7_Adsorption_of_Methane_in_MFI; the reference "
        "contract uses its 300 K, 100000 Pa state point."
    ),
)

_STRUCTURE = SourceRecord(
    key="framework-mfi-si",
    title="MFI_SI framework structure (CIF)",
    path="structures/zeolites/cif/MFI_SI.cif",
    sha256="65ffb4fe61e5c2ad2c5b2a0c452ecb6db8d43faa9c2d172601e594c65e88d9c8",
    bundled_path="structures/MFI_SI.cif",
    citation=(
        "H. van Koningsveld, H. van Bekkum, J. C. Jansen, 'On the location and "
        "disorder of the tetrapropylammonium (TPA) ion in zeolite ZSM-5 with "
        "improved framework accuracy', Acta Cryst. B43, 127-132 (1987)."
    ),
    role="structure",
)

_FORCE_FIELD = SourceRecord(
    key="force-field-example-zeolites",
    title="ExampleZeolitesForceField: force_field_mixing_rules.def",
    path="forcefield/ExampleZeolitesForceField/force_field_mixing_rules.def",
    sha256="9696a6743fa788c3bfb058d0556b696fb5febd6ba769f0e5c7d0f322b4f4a908",
    bundled_path="share/raspa/forcefield/ExampleZeolitesForceField/force_field_mixing_rules.def",
    citation=(
        "P. Bai, M. Tsapatsis, J. I. Siepmann, J. Phys. Chem. C 117, 24375-24387 "
        "(2013); M. G. Martin et al., J. Chem. Phys. 114, 7174-7181 (2001) for CH4."
    ),
    role="force_field",
)

_PSEUDO_ATOMS = SourceRecord(
    key="pseudo-atoms-example-zeolites",
    title="ExampleZeolitesForceField: pseudo_atoms.def",
    path="forcefield/ExampleZeolitesForceField/pseudo_atoms.def",
    sha256="aaab675aedb5357d3b43172b14aba7993b4397395057c953a72e2b2fa9f26138",
    bundled_path="share/raspa/forcefield/ExampleZeolitesForceField/pseudo_atoms.def",
    citation=(
        "Pseudo-atom masses and charges shipped with ExampleZeolitesForceField; "
        "CH4 mass 16.04246 from the same RASPA2 data set."
    ),
    role="force_field",
)

_MOLECULE = SourceRecord(
    key="molecule-methane",
    title="ExampleDefinitions: methane.def",
    path="molecules/ExampleDefinitions/methane.def",
    sha256="5281d172d5c98f8f75e04976a8c13cc3ea6125c56e240ba72e06043e52b1541e",
    bundled_path="share/raspa/molecules/ExampleDefinitions/methane.def",
    citation=(
        "ExampleDefinitions/methane.def; critical constants 190.564 K, "
        "4599200 Pa, acentric factor 0.01142 as stated in the file."
    ),
    role="molecule_definition",
)

REFERENCE_OUTPUT = SourceRecord(
    key="reference-output-100000",
    title="Official RASPA 2.0.45 output for methane in MFI at 100000 Pa",
    path=(
        "examples/Basic/7_Adsorption_of_Methane_in_MFI/Output/System_0/"
        "output_MFI_SI_2.2.2_300.000000_100000.data"
    ),
    sha256="aac482cb8e8e3275063362316d68c160e30803eef35391976d81df953bf84ffd",
    bundled_path="",
    citation=(
        "Unmodified output committed by the RASPA authors; a reference fixture "
        "for parser verification, not a RAVEL execution."
    ),
    role="reference_output",
)


#: Every official source the package and the trace can name. Order is stable
#: so that canonical serialisation is stable.
SOURCE_RECORDS: tuple[SourceRecord, ...] = (
    _LICENSE,
    _EXAMPLE_INPUT,
    _STRUCTURE,
    _FORCE_FIELD,
    _PSEUDO_ATOMS,
    _MOLECULE,
    REFERENCE_OUTPUT,
)

SOURCE_KEYS: tuple[str, ...] = tuple(record.key for record in SOURCE_RECORDS)
_BY_KEY = {record.key: record for record in SOURCE_RECORDS}


def official_source_document(key: str) -> dict[str, str]:
    """One source record, for a manifest.

    Raises:
        KeyError: No source is pinned under this key. Callers pass constants
            from this module, so an unknown key is a programming error rather
            than a contract value.
    """
    return _BY_KEY[key].document()


def source_record(key: str) -> SourceRecord:
    """The immutable record for one pinned source."""
    return _BY_KEY[key]


def read_bundled(key: str, *, root: Path | None = None) -> bytes:
    """Read one bundled file and prove it is the pinned bytes.

    Raises:
        BundledDataError: The file is absent, or its digest is not the pinned
            one. Both kinds say which file and which digest was expected.
    """
    record = _BY_KEY[key]
    if not record.bundled_path:
        raise BundledDataError(
            "NOT_BUNDLED",
            f"source {key} is a record of an official output, not a file this "
            "package prepares a run from",
        )
    base = Path(root) if root is not None else BUNDLED_DATA_ROOT
    path = base / record.bundled_path
    if not path.is_file():
        raise BundledDataError(
            "MISSING",
            f"the bundled RASPA source {key} is not at {path}; the package is "
            "built only from the pinned MIT-licensed reference data",
        )
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != record.sha256:
        raise BundledDataError(
            "MISMATCH",
            f"the bundled RASPA source {key} at {path} hashes to {digest}, not "
            f"the pinned {record.sha256}; the bytes are not the ones this "
            "package's provenance record names",
        )
    return data


class SourceKind(StrEnum):
    """Where a parser's bytes came from.

    Only `REAL_RUN` may produce a `COMPUTATION`. The other two are fixtures:
    one is an official output the software's authors committed, the other is a
    hand-written file shaped like output to exercise a failure branch. Neither
    is a RAVEL run, and both are marked `SIMULATED` in the result.
    """

    REAL_RUN = "REAL_RUN"
    REFERENCE_FIXTURE = "REFERENCE_FIXTURE"
    TEST_FIXTURE = "TEST_FIXTURE"
