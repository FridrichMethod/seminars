"""Tests for meeting codes and telephone numbers written in languages other than English.

Meeting invitations of Zoom, Tencent Meeting and others come in the language
of whoever sends them. Values that look real are assembled in the tests, so
that this file holds no code or number that a scanner would report.
"""

from __future__ import annotations

import pytest

from seminars.pipeline.privacy import PHONE_REMOVED, VALUE_REMOVED, scrub_private, scrub_title

CODE = "65" + "4321"
WORD_CODE = "abc" + "123"
MOBILE = "138" + "1234" + "5678"

PASSWORD_ZH = "\u5bc6\u7801"
PASSWORD_ZH_HANT = "\u5bc6\u78bc"
MEETING_ZH = "\u4f1a\u8bae"
JOIN_ZH = "\u5165\u4f1a"
NUMBER_ZH = "\u53f7"
IS_ZH = "\u662f"
COLON = "\uff1a"
COMMA = "\uff0c"
STOP = "\u3002"
CRYPTOGRAPHY_ZH = "\u5bc6\u7801\u5b66"
PASSWORD_JA = "\u30d1\u30b9\u30ef\u30fc\u30c9"
PASSCODE_JA = "\u30d1\u30b9\u30b3\u30fc\u30c9"
PASSWORD_KO = "\ube44\ubc00\ubc88\ud638"
CONTRASENA = "Contrase\u00f1a"
A_HISTORY_ZH = "\u4e00\u90e8\u5386\u53f2"


class TestCodesInOtherLanguages:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (f"{PASSWORD_ZH}{COLON}{CODE}", f"{PASSWORD_ZH}{COLON}{VALUE_REMOVED}"),
            (f"{PASSWORD_ZH}: {WORD_CODE}", f"{PASSWORD_ZH}: {VALUE_REMOVED}"),
            (
                f"{MEETING_ZH}{NUMBER_ZH} 123 456 789 {PASSWORD_ZH} {CODE}",
                f"{MEETING_ZH}{NUMBER_ZH} 123 456 789 {PASSWORD_ZH} {VALUE_REMOVED}",
            ),
            (
                f"Zoom {MEETING_ZH} ID{COLON}912 3456 7890{COMMA}{PASSWORD_ZH}{COLON}{WORD_CODE}",
                f"Zoom {MEETING_ZH} ID{COLON}912 3456 7890{COMMA}{PASSWORD_ZH}{COLON}"
                f"{VALUE_REMOVED}",
            ),
            (
                f"{MEETING_ZH}{PASSWORD_ZH}{COLON}8888{COMMA}{JOIN_ZH}",
                f"{MEETING_ZH}{PASSWORD_ZH}{COLON}{VALUE_REMOVED}{COMMA}{JOIN_ZH}",
            ),
            (f"{JOIN_ZH}{PASSWORD_ZH}: 4321", f"{JOIN_ZH}{PASSWORD_ZH}: {VALUE_REMOVED}"),
            (f"{PASSWORD_ZH}{IS_ZH}{CODE}{STOP}", f"{PASSWORD_ZH}{IS_ZH}{VALUE_REMOVED}{STOP}"),
            (f"{PASSWORD_ZH_HANT}{COLON}{CODE}", f"{PASSWORD_ZH_HANT}{COLON}{VALUE_REMOVED}"),
            (f"\u53e3\u4ee4{COLON}{CODE}", f"\u53e3\u4ee4{COLON}{VALUE_REMOVED}"),
            (f"{PASSWORD_JA}{COLON}{CODE}", f"{PASSWORD_JA}{COLON}{VALUE_REMOVED}"),
            (f"{PASSCODE_JA}: {WORD_CODE}", f"{PASSCODE_JA}: {VALUE_REMOVED}"),
            (f"{PASSWORD_KO}: {CODE}", f"{PASSWORD_KO}: {VALUE_REMOVED}"),
            (f"Kennwort: {CODE}", f"Kennwort: {VALUE_REMOVED}"),
            (f"Passwort: {WORD_CODE}", f"Passwort: {VALUE_REMOVED}"),
            (f"{CONTRASENA}: {CODE}", f"{CONTRASENA}: {VALUE_REMOVED}"),
            (f"Mot de passe : {CODE}", f"Mot de passe : {VALUE_REMOVED}"),
            (f"Senha: {CODE}", f"Senha: {VALUE_REMOVED}"),
        ],
    )
    def test_the_code_goes_and_the_label_stays(self, text: str, expected: str) -> None:
        assert scrub_private(text) == expected
        assert scrub_private(expected) == expected

    @pytest.mark.parametrize(
        "text",
        [
            f"{CRYPTOGRAPHY_ZH}{COLON}{A_HISTORY_ZH}",
            f"{CRYPTOGRAPHY_ZH} 101",
            f"{MEETING_ZH}{NUMBER_ZH}{COLON}123 456 789",
            "Kennwort und Sicherheit",
            "Mot de passe oubli\u00e9",
        ],
    )
    def test_words_about_passwords_stay(self, text: str) -> None:
        assert scrub_private(text) == text

    def test_a_title_keeps_a_label_with_a_word_after_it(self) -> None:
        title = f"{PASSWORD_ZH}{COLON}{A_HISTORY_ZH}"
        assert scrub_title(title) == title
        assert scrub_title(f"Seminar {PASSWORD_ZH}{COLON}{CODE}") == (
            f"Seminar {PASSWORD_ZH}{COLON}{VALUE_REMOVED}"
        )


class TestMobileNumbersOfChina:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (MOBILE, PHONE_REMOVED),
            (f"\u7535\u8bdd {MOBILE}{STOP}", f"\u7535\u8bdd {PHONE_REMOVED}{STOP}"),
            (f"+86 {MOBILE}", PHONE_REMOVED),
        ],
    )
    def test_a_mobile_number_goes(self, text: str, expected: str) -> None:
        assert scrub_private(text) == expected

    @pytest.mark.parametrize(
        "text",
        ["Event 53759577652952", "doi:10.1038/13812345678", "id=13812345678", "1381234567"],
    )
    def test_other_long_numbers_stay(self, text: str) -> None:
        assert scrub_private(text) == text
