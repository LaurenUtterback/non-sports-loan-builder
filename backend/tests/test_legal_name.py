"""The borrower's name is their FULL legal name (Lauren, 2026-10-01).

A memo named the borrower by the two-part name the contract and the PFS used,
while the passport and the driver's license in the same file carried a
compound surname. Government ID now governs the name on every document the app
prints, which these lock in two places:

1. the rule reaches ALL FIVE uploaders, so the four tabs cannot disagree about
   what the borrower is called;
2. the UCC-1's debtor boxes keep a compound surname whole and put a
   generational suffix in its own box - half a surname (or a "Jr.") in box 1b
   is what makes a financing statement defective.
"""

from app import (binder_extraction, extraction, loandocs_extraction,
                 pa_extraction, structure_extraction)
from app.loandocs import _split_name, render_html
from app.loandocs_models import LoanDocsInclude, LoanDocTerms


def test_every_uploader_carries_the_full_legal_name_rule():
    for module in (extraction, structure_extraction, pa_extraction,
                   loandocs_extraction, binder_extraction):
        assert extraction.LEGAL_NAME_RULE in module.PROMPT, module.__name__


def test_the_memo_reader_keeps_the_name_the_memo_prints():
    # The Loan Documents tab can read a finished memo instead of the deal
    # documents; that name is already the full legal one.
    assert "copied EXACTLY as the memo prints it" in loandocs_extraction.MEMO_PROMPT


def test_the_extraction_asks_for_the_ids_surname_separately():
    assert '"borrower_surname":null' in extraction.PROMPT
    assert '"player_surname":null' in loandocs_extraction.PROMPT


def test_ucc_keeps_a_compound_surname_whole():
    # The ID's surname field decides; "Delgado" alone would be a defective
    # filing against a debtor whose licence reads RIVAS DELGADO / MATEO.
    assert _split_name("Mateo Rivas Delgado", "Rivas Delgado") \
        == ("Rivas Delgado", "Mateo", "")


def test_ucc_moves_a_generational_suffix_out_of_the_surname_box():
    assert _split_name("Jalen Two-Rivers Jr.") == ("Two-Rivers", "Jalen", "Jr.")
    assert _split_name("Jalen A. Two-Rivers Jr.", "Two-Rivers") \
        == ("Two-Rivers", "Jalen", "A. Jr.")


def test_ucc_falls_back_to_the_last_word_without_an_id_surname():
    # Nothing in the string says where a compound surname begins, so the old
    # behaviour stands and the extra name part lands in the middle-name box
    # (which the underwriter can correct on the tab).
    assert _split_name("Mateo Rivas Delgado") == ("Delgado", "Mateo", "Rivas")
    assert _split_name("Jalen Two-Rivers") == ("Two-Rivers", "Jalen", "")
    assert _split_name("") == ("", "", "")


def test_ucc_tolerates_a_name_typed_without_the_second_surname():
    assert _split_name("Mateo", "Rivas Delgado") == ("Rivas Delgado", "Mateo", "")


def _terms(**kw) -> LoanDocTerms:
    return LoanDocTerms(borrower_name="Mateo Rivas Delgado", **kw)


def test_rendered_ucc_boxes_carry_the_whole_surname():
    html = render_html(_terms(borrower_surname="Rivas Delgado"),
                       LoanDocsInclude())
    assert "<br>Rivas Delgado</td>" in html          # 1b. LAST NAME
    assert "<br>Mateo</td>" in html                  # FIRST NAME
    # The full legal name still prints on every other document in the package.
    assert "Mateo Rivas Delgado" in html


def test_rendered_ucc_suffix_box_is_filled():
    html = render_html(LoanDocTerms(borrower_name="Jalen Two-Rivers Jr."),
                       LoanDocsInclude())
    assert "<br>Two-Rivers</td>" in html
    assert "<br>Jr.</td>" in html
