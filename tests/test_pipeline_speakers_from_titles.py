"""Tests for the speakers that normalization reads from a title.

A draft without a speaker (every record of the campus calendar, whose API has
no speaker names) gets the people its title names in one of a few common
forms. The titles below are real ones of events.stanford.edu.
"""

from __future__ import annotations

import pytest

from seminars.pipeline.speakers import ParsedSpeaker, speakers_from_title

ENERGY_FAN = (
    "Stanford Energy Seminar | Electrified metamaterial reactors for process intensified"
    " thermochemistry | Jonathan Fan, Stanford University"
)
ENERGY_DIAZ_MARIN = (
    "Stanford Energy Seminar | Soft Materials for Hard Sustainability Challenges: Water"
    " production, Data center Sustainability, and High-Temperature Heat | Carlos Diaz-Marin,"
    " Stanford University"
)
MICROBIOLOGY = "Microbiology & Immunology Wednesday Seminar: "
BIO_FAN = "Jonathan Fan is a professor of electrical engineering at Stanford University."
BIO_DIAZ_MARIN = "Carlos Diaz-Marin is a postdoctoral fellow at Stanford University."
BIO_WOJCICKI = (
    "This instance of the Medicine Grand Rounds series will feature Anne Wojcicki, who"
    " co-founded 23andMe."
)
BIO_LIN_PALANIAPPAN = (
    "Leading cancer researcher Bryant Lin, MD, distills the lessons for living. He will be"
    " joined by Latha Palaniappan, MD, MS, co-director, Center for Asian Health."
)


@pytest.mark.parametrize(
    ("title", "abstract", "people"),
    [
        # Seventeen titles of the campus calendar. Two start with a series name that reads
        # like a name; their speaker is the last part.
        (ENERGY_FAN, BIO_FAN, [("Jonathan Fan", "Stanford University")]),
        (ENERGY_FAN, None, [("Jonathan Fan", "Stanford University")]),
        (ENERGY_DIAZ_MARIN, BIO_DIAZ_MARIN, [("Carlos Diaz-Marin", "Stanford University")]),
        (
            MICROBIOLOGY + 'Britt Glaunsinger - "Polymerase crosstalk shapes inflammatory gene'
            ' expression during viral infection"',
            None,
            [("Britt Glaunsinger", None)],
        ),
        (MICROBIOLOGY + "Oscar Aguilar", None, [("Oscar Aguilar", None)]),
        (MICROBIOLOGY + "Katie Pollard", None, [("Katie Pollard", None)]),
        (MICROBIOLOGY + "Janelle Ayres", None, [("Janelle Ayres", None)]),
        ("Grand Rounds: Gaurav Parmar", None, [("Gaurav Parmar", None)]),
        (
            "PhD Dissertation Defense: Alyssa Cutter, Bando Lab",
            None,
            [("Alyssa Cutter", "Bando Lab")],
        ),
        (
            'Geophysics Seminar - Shang-Ping Xie, "What powered the growth of 2026 super El'
            ' Ni\u00f1o?"',
            None,
            [("Shang-Ping Xie", None)],
        ),
        (
            "Stanford Cancer Institute Breakthroughs in Cancer: Janice Mehnert, MD",
            None,
            [("Janice Mehnert", None)],
        ),
        ("Fireside Chat with Anne Wojcicki", BIO_WOJCICKI, [("Anne Wojcicki", None)]),
        ("Evolgenome Seminar: Ami Bhatt", None, [("Ami Bhatt", None)]),
        (
            "Sunshine - Bryant Lin in conversation with Latha Palaniappan",
            BIO_LIN_PALANIAPPAN,
            [("Bryant Lin", None), ("Latha Palaniappan", None)],
        ),
        (
            'CSCS William C. Dement Seminar Series: " Cortical inhibitory circuits regulating'
            ' brain states and sleep" with Renata Batista-Brito, Ph.D.',
            None,
            [("Renata Batista-Brito", None)],
        ),
        (
            'CSCS Community Series: "Effects of oligodendroglia and myelin integrity on sleep"'
            " with Rebecca Buchanan, PhD",
            None,
            [("Rebecca Buchanan", None)],
        ),
        ("PhD Dissertation Defense: Danielle Klinger", None, [("Danielle Klinger", None)]),
        (
            'Geophysics Seminar - Tapan Mukerji: "Exploring Quantum Computing in the'
            ' Geosciences: The Good, the Bad, and the Noisy"',
            None,
            [("Tapan Mukerji", None)],
        ),
    ],
)
def test_the_titles_of_the_campus_calendar(
    title: str, abstract: str | None, people: list[tuple[str, str | None]]
) -> None:
    found = speakers_from_title(title, abstract)
    assert found == [ParsedSpeaker(name, affiliation) for name, affiliation in people]
    assert all(person.name != "Stanford Energy Seminar" for person in found)


@pytest.mark.parametrize(
    ("title", "abstract", "people"),
    [
        (
            "Jonathan Kipnis - Navigating turbulent CNS waters",
            "Navigating turbulent CNS waters\nAbstract\nThe central nervous system ...\n"
            "Jonathan Kipnis, PhDAlan A. and Edith L. Wolff Distinguished Professor",
            [("Jonathan Kipnis", None)],
        ),
        (
            "Michael Hasselmo | Memory encoding, retrieval and planning of spatiotemporal"
            " trajectories",
            "MBCT presents a seminar with Dr. Michael Hasselmo.\nMichael Hasselmo, Professor,"
            " Boston University",
            [("Michael Hasselmo", None)],
        ),
        (
            "Simon DeDeo | Proofs and reasons in the age of artificial intelligence",
            "Meet the SpeakerSimon DeDeo",
            [("Simon DeDeo", None)],
        ),
        (
            "Jane Doe (MIT): Knots and their invariants",
            None,
            [("Jane Doe", "MIT")],
        ),
        (
            "Jane Doe (Stanford University): Knots and their invariants",
            None,
            [("Jane Doe", "Stanford University")],
        ),
        ("Seminar: \u5f20\u4e09", None, [("\u5f20\u4e09", None)]),
        ("Colloquium - Prof. Jane Doe, PhD, MIT", None, [("Jane Doe", "MIT")]),
    ],
)
def test_a_name_first_title_needs_its_abstract_or_an_affiliation(
    title: str, abstract: str | None, people: list[tuple[str, str | None]]
) -> None:
    found = speakers_from_title(title, abstract)
    assert found == [ParsedSpeaker(name, affiliation) for name, affiliation in people]


@pytest.mark.parametrize(
    ("title", "abstract"),
    [
        # Series names and talk titles that read like names.
        *[(title, None) for title in ["Norton Lecture", "Salvatierra Lecture", "ACS Grand Rounds"]],
        ("Professional Development Grand Rounds", None),
        ("PhD Dissertation Defense", None),
        ("Health Equity Innovation Series", None),
        ("Geophysics Seminar", None),
        ("Sabine Kastner", "Sabine KastnerPrinceton University"),
        (MICROBIOLOGY + "Relman & Carette Labs", None),
        ("Beyond Checkpoint Blockade: Vaccines and the Next Era of Melanoma Immunotherapy", None),
        (
            "Beyond Checkpoint Blockade: Vaccines and the Next Era of Melanoma Immunotherapy",
            "Beyond Checkpoint Blockade: Vaccines and the Next Era of Melanoma Immunotherapy",
        ),
        ("Long-Context Structure Prediction with ESMFold2 Webinar", None),
        ("Learning with Graph Neural Networks", None),
        ("Quantum Seminar: New Frontiers", None),
        ("Deep Learning: Theory And Practice", None),
        ("Topology Seminar: Speaker TBA", None),
        ("Algebra Seminar: TBD", None),
        ("A Conversation with Researchers: Tracking HIV Through Wastewater in South Africa", None),
        (
            "Distinguished Lecture Series| From Disentanglement to Re-Entanglement: Integrating"
            " the Living Archaeological Record",
            None,
        ),
        ("Shoot for the Stars: A community-based approach to Sunflower Star Recovery", None),
        ("Anesthesiology Grand Rounds with the California Surgeon General", None),
        ("Protein Design: Jane Doe", None),
        ("Geophysics Seminar | Learning on graphs | Graph Neural Networks", None),
        ("Stanford Energy Seminar | Grid storage | Stanford Energy Seminar", None),
        ("Machine Learning - Graph Neural Networks", "Graph Neural Networks are popular."),
        ("", None),
        ("TBA", None),
        # Topics that read like names after a series name.
        ("Physics Colloquium: Dark Matter", None),
        ("Topology Seminar: Floer Homology", None),
        ("Statistics Seminar: Causal Inference", None),
        ("Workshop: Deep Learning", None),
        ("Math Colloquium: Random Matrices", None),
        ("Seminar: Markov Chains", None),
        ("Seminar: Monte Carlo, MIT", None),
        ("Seminar | Earthquake Science | Plate Boundaries, Stanford University", None),
        ("Analysis Seminar: Riemannian Geometry", None),
        ("Probability Seminar - Gaussian Processes", None),
        ("Colloquium: Black Holes", None),
        ("Algebra Seminar: Quantum Groups", None),
        ("Seminar: Optimal Transport", None),
        ("Applied Physics Colloquium: Topological Insulators", None),
        ("Biology Seminar: Membrane Biophysics", None),
        ("Seminar: Spectral Theory", None),
        ("Seminar: Graph Neural Networks, Google", None),
        ("Seminar: Causal Inference", "We study causal inference in observational data."),
        ("Seminar: Emergent Properties", None),
        ("Seminar: Korean Indigenous Futures", None),
        # Events and titles that read like names after a series name.
        ("Library Series: Coffee Hour", None),
        ("Biology Seminar: Winter Quarter", None),
        ("History Lecture: A Closer Look", None),
        ("Book Talk | Living Minerals | Pattern Magic", None),
    ],
)
def test_titles_that_name_no_speaker(title: str, abstract: str | None) -> None:
    assert speakers_from_title(title, abstract) == []


LUNCH_CLUB = (
    "Lunch Club Series | Navigating Heritage: Chinook Art and Public History Along the"
    " Columbia River"
)


@pytest.mark.parametrize(
    ("title", "abstract", "people"),
    [
        (LUNCH_CLUB, "On Nov 4, 2026, we will host Dr.Jon Daehnke from UC Santa Cruz.", []),
        (LUNCH_CLUB, None, []),
        ("Building Bridges Seminar Series: Building Bridges", None, []),
        ("Media Lecture Series: Channing Dungey", None, []),
        (
            "Media Lecture Series: Channing Dungey",
            "Channing Dungey is the chair of a television group.",
            [("Channing Dungey", None)],
        ),
    ],
)
def test_a_name_that_starts_with_a_verb_in_ing_needs_its_abstract(
    title: str, abstract: str | None, people: list[tuple[str, str | None]]
) -> None:
    found = speakers_from_title(title, abstract)
    assert found == [ParsedSpeaker(name, affiliation) for name, affiliation in people]


@pytest.mark.parametrize(
    ("title", "abstract", "people"),
    [
        # Names whose last word ends like a word of a field, but is a name.
        ("Seminar: Neil Lawrence", None, [("Neil Lawrence", None)]),
        ("Organic Chemistry Seminar: Huw Davies", None, [("Huw Davies", None)]),
        ("Seminar: Monte Westerfield", None, [("Monte Westerfield", None)]),
        ("Seminar: Igor Markov", None, [("Igor Markov", None)]),
        ("Seminar: Hugo de Vries", None, [("Hugo de Vries", None)]),
        ("Seminar: Paul Clement", None, [("Paul Clement", None)]),
        # A name that reads like a topic is a speaker when the abstract names the person.
        ("Seminar: Jane Matter", None, []),
        (
            "Seminar: Jane Matter",
            "Jane Matter is a professor of physics at MIT.",
            [("Jane Matter", None)],
        ),
        (
            "Colloquium: Sam Learning",
            "Dr. Sam Learning studies proteins.",
            [("Sam Learning", None)],
        ),
    ],
)
def test_a_name_that_reads_like_a_topic_needs_its_abstract(
    title: str, abstract: str | None, people: list[tuple[str, str | None]]
) -> None:
    found = speakers_from_title(title, abstract)
    assert found == [ParsedSpeaker(name, affiliation) for name, affiliation in people]
