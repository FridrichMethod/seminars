"""Privacy of free text: no meeting credentials, e-mail addresses or telephone numbers.

data/ is committed to a public repository and everything published is
built from it, so normalization takes out of every text field what would
let a stranger into a meeting or reach a person:

* credentials in links. In every link of a text (``https://...``, or a
  bare ``host.edu/path?query``) the query and fragment parameters named
  like a credential (``CREDENTIAL_PARAMETERS``: ``pwd``, ``passcode``,
  ``pass``, ``tk``, ``token`` and the like, also percent-encoded as in
  ``pwd%3D...``) are dropped and the rest of the link stays, so a meeting
  link still leads to its meeting, which then asks for the code. A link
  with a user name or password in it, or with an e-mail address, becomes
  ``[link removed]``, and so does a link that wraps another link with a
  credential: a redirect (``google.com/url?q=...``), a protection service
  (Outlook safelinks, Proofpoint ``urldefense``), percent-encoded once or
  twice;
* written-out codes. The value after "Passcode:", "Passcodes:",
  "Password:", "pwd=", "Meeting password:", "Password if prompted:",
  "Passcode (case sensitive):", "PIN:", "Passcode - ", "the passcode is",
  "the passcode for the meeting is" becomes ``[removed]`` and the label
  stays; quotes or brackets around the value go with it, and so do groups
  of digits ("12 34 56"). A value with a digit is always taken. One
  without a digit is taken after "Passcode:" and the like; after
  "Password:", "PW=" and "PIN:", and after a label without a colon
  ("Passcode 'abc'", "the meeting password is abc", "use password abc"),
  only when it is not a common word ("required", "protected", "the") and
  ends its line, the text or a sentence, or is followed by a closing
  bracket, a link or another label ("Meeting ID", "Passcode", "Join", "to
  join"). So "PIN: auxin efflux carriers", "the password: a history" and
  "Zoom password protected" stay, and after a bare "password" without a
  colon a value needs a digit ("the password is the weakest link"). After
  a label without a colon the value is also no word of prose about
  passwords ("use password managers", "enter PIN codes", "type the
  password correctly") and does not end a question ("Is the passcode
  secret?"); "use" and "enter" take one code ("use passwords safely"
  stays). A meeting id ("Meeting ID: 984 6689 7972") stays: it lets nobody
  in without the code. The words for password in German, Spanish, French
  and Portuguese ("Kennwort:", "Mot de passe :") are labels like
  "Password:". The words for password, passcode and PIN of Chinese,
  Japanese and Korean invitations are labels like "Passcode:": any value
  after a colon (also the full-width one) goes, and a value with a digit
  after a space or after the word for "is". These scripts put no space
  between words, so such a label also counts inside a longer word (the
  one for meeting password); the word for cryptography, which starts with
  the one for password, is no label, because no separator follows.
  Full-width punctuation ends a value. A title or a series name is
  scrubbed by ``scrub_title``, which leaves codes without a digit alone
  (after these labels all of them): there such a
  value is a word of what the talk is about ("Password: a history of
  authentication", "How users enter PIN codes"). Only the one word after
  "Passcode:", "Zoom password:" and the like that ends the title (or its
  brackets) goes ("Seminar - Zoom passcode: neuro"), unless it is a common
  word or a word of prose about passwords ("Passcodes: Security");
* e-mail addresses, also written as "name @ host.edu", with a full-width
  at sign, as "name [at] host [dot] edu" or "name at host dot edu",
  become ``[email removed]``; so does "name at host.edu" after "E-mail",
  "Contact", "Write to" or "RSVP to", or when the name has a letter and a
  dot, a digit, an underscore, "%" or "+" and the host is one people have
  mailboxes at: a university or an agency itself ("j.doe at stanford.edu",
  "jdoe2 at ox.ac.uk") or a mail provider ("jane.doe at gmail.com"); a
  department of one ("jane.doe at cs.stanford.edu", "jdoe2 at
  csail.mit.edu", "j.doe at eng.cam.ac.uk") when the name has no capital
  or two letters joined by a dot, "_" or "+"; and any host in a country
  domain or at ".com", ".org" or ".net" ("jane.doe at ethz.ch", "j_doe at
  genentech.com") for such a joined name. A host whose labels have a
  digit or start with a word of a web site ("events.stanford.edu",
  "cs229.stanford.edu", "www.ethz.ch"), a time, a number, an abbreviation
  and a file or package name with a known ending (".pdf", ".mp4", ".js",
  ".py" and the others of ``_MAILBOX``) are no address, so "Recordings of
  CS224N at youtube.com", "CS224N at cs.stanford.edu", "closes at 5pm at
  eventbrite.com", "posted on Nov. 12 at stanford.edu", "e.g. at
  stanford.edu", "talk.mp4 at youtu.be" and "node.js at github.com" stay.
  A plain "jdoe at stanford.edu" stays too: "Register at
  events.stanford.edu" reads the same. The boundary is drawn on the side
  of removing too much. Known removals of text that is no address: a
  module path or a joined name at a ".org" or ".com" host ("torch.nn at
  pytorch.org", "sklearn.svm at scikit-learn.org", "research.google at
  google.com") and a lower-case course code or "Ph.D" at a department
  ("cs229 at cs.stanford.edu", "Ph.D at cs.stanford.edu"). Known keeps of
  what may be one: a local part with a capital and a digit at a department
  ("Jdoe2 at cs.stanford.edu") and a host with more than three labels
  before the university ("jane.doe at a.b.c.d.stanford.edu").
  One in brackets after a name ("Jane Doe (jdoe@stanford.edu)") is
  removed with its brackets, so that the line still splits into a name
  and nothing else. File names ("photo@2x.png") and the ids of shared
  calendars and chat threads ("...@group.calendar.google.com",
  "...@thread.skype") are no addresses, in a text or in a link;
* telephone numbers become ``[phone removed]``: North American numbers with
  separators ("(650) 723-1234", "650.723.1234", "650-7231234",
  "1-800-555-0199") or without ("6507231234"), international numbers that
  start with "+", a one-tap dial string with everything after the number
  ("+16507249799,,98479731295#,,,,*123456#"), mobile numbers of mainland
  China (eleven digits that start with 13 to 19, also right after CJK
  text), any number after "Phone",
  "Tel.", "Fax", "Call", "Text", "Contact", "Cell:" and the like (a campus
  extension "3-1234" too), and a campus number after "at" in a sentence
  about reaching someone ("Contact Jane Doe at 723-1234"). Dates, year
  ranges, DOIs, arXiv ids, citations ("Cell (2019) 176: 1234"), room
  numbers and meeting ids are not telephone numbers.

A speaker line is not prose, and a marker there would be read as an
affiliation. ``strip_markers`` takes the markers out of a speaker line
before it is split and out of a speaker's name and affiliation, with the
cue in front of each ("Phone:", "E-mail:") and the commas, brackets and
lines that are left empty.

Every rule is a regular expression whose quantifiers are bounded (runs of
white space between the words of a label too) or cannot backtrack far, and
the rules run over the text a bounded number of times, so the time is
linear in the length of the text. Scrubbing its own result changes
nothing: the markers contain nothing any rule looks for. Text without any
of these things comes back unchanged, which is why names are never
altered.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import unquote, urlsplit

from seminars.models import CREDENTIAL_QUERY_PARAMS

LINK_REMOVED: Final = "[link removed]"
VALUE_REMOVED: Final = "[removed]"
EMAIL_REMOVED: Final = "[email removed]"
PHONE_REMOVED: Final = "[phone removed]"

CREDENTIAL_PARAMETERS: Final[frozenset[str]] = CREDENTIAL_QUERY_PARAMS | frozenset(
    {"pass", "passwd", "pw", "pin", "mtid", "uname"}
)
"""Query and fragment parameters dropped from links: credentials, and a Zoom display name."""

MAX_PASSES: Final = 3
"""How often the rules run over one text at most (see ``scrub_private``)."""

_TRAILING: Final = ".,;:!?"

# --- links ---------------------------------------------------------------------------------

_LINK: Final = re.compile(
    r"(?:https?://|(?<![\w.@/%+-])(?:[a-z0-9-]{1,63}\.){1,10}[a-z]{2,24}/)[^\s<>\"')\]]*",
    re.IGNORECASE,
)

_CREDENTIAL_NAMES: Final = "|".join(sorted(re.escape(name) for name in CREDENTIAL_PARAMETERS))
_NESTED_CREDENTIAL: Final = re.compile(rf"[?&;#](?:{_CREDENTIAL_NAMES})=", re.IGNORECASE)
_NESTED_LOGIN: Final = re.compile(r"://[^/?#\s@:]{1,256}:[^/?#\s@]{1,256}@")
_PROOFPOINT_ESCAPE: Final = re.compile(r"-([0-9A-Fa-f]{2})")
_PROOFPOINT_HOST: Final = "urldefense"

# --- e-mail addresses ----------------------------------------------------------------------

_AT: Final = r"(?:\s?[@\uff20]\s?|\s?[\[({]\s?at\s?[\])}]\s?)"
_DOT: Final = r"(?:\.|\s?[\[({]\s?dot\s?[\])}]\s?)"
_SERVICE_HOST: Final = (
    r"(?!(?:[\w-]{1,63}\.){0,8}(?:(?:group|resource|import)\.calendar\.google\.com"
    r"|thread\.skype)(?![\w.-]))"
)
"""Hosts of ids that are written like an address: calendars, chat threads."""
_FILE_ENDING: Final = r"(?!(?:png|jpe?g|gif|svg|webp|pdf|ics|html?|php|aspx?)(?![\w-]))"
"""Endings of file names ("photo@2x.png"); no top-level domain is one of them."""
_ADDRESS: Final = (
    rf"(?<![\w.%+-])[\w.%+-]{{1,64}}{_AT}{_SERVICE_HOST}"
    rf"(?:[\w-]{{1,63}}{_DOT}){{1,8}}{_FILE_ENDING}[a-z]{{2,24}}(?![\w-])"
)
_TOP_LEVEL: Final = r"(?:edu|com|org|net|gov|io|us|uk|ca|de|fr|ch|cn|jp)"
_NOT_A_HOST: Final = r"(?!(?:the|a|an|this|that|these|our|your|my|its|their)\s)"
_SPELLED_ADDRESS: Final = (
    r"(?<![\w.%+-])[\w.%+-]{1,64}\s{1,3}at\s{1,3}"
    rf"(?:{_NOT_A_HOST}[\w.-]{{1,63}}\s{{1,3}}dot\s{{1,3}}){{1,8}}{_TOP_LEVEL}(?![\w-])"
)
_MAIL_CUE: Final = (
    r"\b(?:e-?mail(?:\s{1,3}address)?|contact|write\s{1,3}to|rsvp(?:\s{1,3}to)?)\b\.?"
    r"\s{0,3}[:-]?\s{0,3}"
)
_MAILBOX: Final = (
    r"(?<![\w.%+-])(?=[\w.%+-]{0,63}[._%+\d])(?=[\w.%+-]{0,63}[^\W\d_])"
    r"(?!\d{1,2}(?:[:.]\d{2})?(?:am|pm)(?![\w.%+-]))"
    r"(?![\w.%+-]{0,59}\.(?:pdf|pptx?|key|docx?|mp[34]|mov|m4v|png|jpe?g|gif|svg|zip|html?"
    r"|js|py|ts|io|net|ai|md|txt|csv|json|ipynb)\s)"
)
"""A local part with a letter and a dot, an underscore, a digit, "%" or "+".

A time ("5pm") and a file or package name with one of the endings listed
here ("talk.mp4", "node.js") are no local part. Any other dotted name is
one: "torch.nn" and "numpy.linalg" read like "jane.doe".
"""
_PERSON: Final = r"(?=[\w.%+-]{0,61}[^\W\d_][._+][^\W\d_])"
"""A local part with two letters joined by a dot, "_" or "+" ("jane.doe", "j_doe")."""
_LOWER: Final = r"(?=(?-i:[^\W_A-Z]|[._%+-]){1,64}\s)"
"""A local part without a capital letter: "jdoe2" is a mailbox, "CS224N" a course."""
_LOCAL: Final = r"[\w.%+-]{1,64}(?<!\.)\s{1,3}at\s{1,3}"
"""A local part and the spelled "at"; a local part never ends in a dot ("e.g. at")."""
_UNIVERSITY: Final = r"[\w-]{1,63}\.(?:edu|gov|(?:ac|edu)\.[a-z]{2})"
_PROVIDER: Final = (
    r"(?:gmail|googlemail|outlook|hotmail|live|yahoo|icloud|me|protonmail|proton|aol"
    r"|qq|163|126)\.(?:com|me|net|cn)"
)
_WEB_LABEL: Final = (
    r"(?!(?:www\d{0,2}|w3|web|events?|calendars?|news|sites?|librar(?:y|ies)|lib|profiles?"
    r"|people|apps?|docs|blogs?|wiki|videos?|media|online|login|search|courses?|class(?:es)?"
    r"|cms|portal|static|cdn|go|my)\.)"
)
"""First labels of a host that serves pages ("events.stanford.edu", "www.ethz.ch")."""
_LABELS: Final = rf"{_WEB_LABEL}(?:[a-z][a-z-]{{0,62}}\.){{1,3}}"
"""One to three labels of letters and hyphens: a department ("cs.", "eng.cam.")."""
_WEB_COUNTRY: Final = r"(?!(?:io|ly|me|tv|ai|co|us|gl|fm|to|gg|sh|so|cc|ws)(?![\w-]))"
"""Country domains used by web services ("bit.ly", "zoom.us") rather than universities."""
_NAMED_HOST: Final = rf"{_LABELS}(?:{_WEB_COUNTRY}[a-z]{{2}}|com|org|net)"
"""A host of letters in a country domain or at ".com", ".org" or ".net"."""
_HOST_END: Final = r"(?![\w-]|\.\w|/)"
_MAIL_HOST: Final = (
    rf"{_MAILBOX}{_LOCAL}(?:{_UNIVERSITY}|{_PROVIDER})"
    rf"|{_MAILBOX}(?:{_PERSON}|{_LOWER}){_LOCAL}{_LABELS}{_UNIVERSITY}"
    rf"|{_MAILBOX}{_PERSON}{_LOCAL}{_NAMED_HOST}"
)
"""A mailbox at a host people have mailboxes at.

That is a university or an agency itself ("stanford.edu", "ox.ac.uk") or a
mail provider, for every local part of ``_MAILBOX``; a department of a
university ("cs.stanford.edu", "eng.cam.ac.uk") for a name ("jane.doe") or
a local part without a capital ("jdoe2"); and a host in a country domain
("ethz.ch", "inf.ethz.ch") or at ".com", ".org" or ".net" ("genentech.com")
for a name. The labels of such a host have no digit and the first is no
word of a web site ("events", "www"), so "EE380 at events.stanford.edu",
"CS229 at cs229.stanford.edu" and "CS224N at cs.stanford.edu" name no
mailbox, and neither does a course or a package at a web site ("CS224N at
youtube.com", "node.js at github.com").
"""
_NOT_A_MAILBOX: Final = (
    r"(?!(?:info|information|details|us|me|them|him|her|page|form|desk|office|here)\s)"
)
_AT_HOST: Final = (
    r"(?P<mail_cue>" + _MAIL_CUE + r")" + _NOT_A_MAILBOX + r"[\w.%+-]{1,64}\s{1,3}at\s{1,3}"
    rf"(?:[\w-]{{1,63}}\.){{1,8}}{_TOP_LEVEL}{_HOST_END}"
    rf"|(?:{_MAIL_HOST}){_HOST_END}"
)
_EMAIL: Final = re.compile(
    rf"(?P<wrapped>\s?[(<\[]\s?(?:mailto:)?{_ADDRESS}\s?[)>\]])"
    rf"|(?:mailto:)?{_ADDRESS}|{_SPELLED_ADDRESS}|{_AT_HOST}",
    re.IGNORECASE,
)

# --- written-out codes ---------------------------------------------------------------------

_STRONG_LABEL: Final = (
    r"(?:(?:meeting|zoom|webinar|webex|teams|participant|attendee|numeric|access)\s{1,3})?"
    r"(?:pass[\s-]?codes?|passwd|pwd)"
    r"|(?:meeting|zoom|webinar|webex|teams|participant|attendee|access)\s{1,3}passwords?"
)
_PASSWORD_WORDS: Final = (
    r"passwords?|passw(?:o|oe)rt|kennw(?:o|oe)rt|contrase\u00f1as?|mots?\s{1,3}de\s{1,3}passe"
    r"|senhas?"
)
"""The word for password in English, German, Spanish, French and Portuguese."""
_WEAK_LABEL: Final = rf"{_PASSWORD_WORDS}|pw|pin"
_CJK_LABEL: Final = (
    r"\u5bc6\u7801|\u5bc6\u78bc|\u53e3\u4ee4"  # password, passphrase (Chinese)
    # password, passcode, PIN (Japanese)
    r"|\u30d1\u30b9\u30ef\u30fc\u30c9|\u30d1\u30b9\u30b3\u30fc\u30c9|\u6697\u8a3c\u756a\u53f7"
    r"|\ube44\ubc00\ubc88\ud638|\uc554\ud638"  # password, code (Korean)
)
"""Labels of a meeting code in Chinese, Japanese and Korean.

These scripts put no space between words, so such a label counts inside a
longer word ("meeting password", "enter the password" are one word) and
needs no word boundary in front; the separator after it keeps the word for
cryptography, which starts with the one for password, apart.
"""
_CJK_IS: Final = r"\s{0,3}[\u662f\u4e3a\u70ba\u306f]\s{0,3}[:\uff1a]?\s{0,3}"
"""The word for "is" after a label in Chinese and Japanese ("the password is 123456")."""
_LABEL_START: Final = r"(?:\b(?:{latin})|(?:" + _CJK_LABEL + "))"
_QUALIFIER: Final = (
    r"(?:\s{0,3}\(?\s{0,3}(?:if\s{1,3}(?:prompted|needed|required|asked)"
    r"|for\s{1,3}(?:zoom|the\s{1,3}(?:meeting|webinar|call)|webinar|meeting)"
    r"|to\s{1,3}join)\s{0,3}\)?"
    r"|\s{0,3}\(\s{0,3}[a-z][\w-]{0,20}(?:\s{1,3}[\w-]{1,20}){0,2}\s{0,3}\))?"
)
_SEPARATOR: Final = r"\s{0,3}(?:[:=#\uff1a]{1,2}|-{1,3}(?=\s)|[\u2010-\u2015\u2212]{1,3})\s{0,3}"
_CJK_PUNCTUATION: Final = "\uff0c\u3002\uff1b\uff1a\uff08\uff09\u3001\uff01\uff1f"
"""Full-width comma, stop, semicolon, colon, brackets, list comma, exclamation and question
marks: they end a value as their ASCII forms do."""
_ENCLOSED: Final = (
    r"\"[^\"\s<>]{1,64}\"|'[^'\s<>]{1,64}'|\u201c[^\u201d\s<>]{1,64}\u201d"
    r"|\u2018[^\u2019\s<>]{1,64}\u2019|\([^()\s<>]{1,64}\)|\[[^\[\]\s<>]{1,64}\]"
    r"|\{[^{}\s<>]{1,64}\}|<[^<>\s]{1,64}>"
)
_DIGITS: Final = (
    r"(?>(?:\d{1,4}(?:\x20\d{1,4}){1,5}(?!\d)|\d+(?:\x20\d{3,4})*)#?)"
    rf"(?![^\s.,;:!?)\]}}{_CJK_PUNCTUATION}])"
)
_WORD: Final = rf"[^\s<>\"'()\[\]{{}},;\u201c\u201d\u2018\u2019{_CJK_PUNCTUATION}]+"
_VALUE: Final = rf"(?:{_ENCLOSED}|{_DIGITS}|{_WORD})"
_HAS_DIGIT: Final = r"(?=[^\s<>]{0,64}\d)"
_STRICT_SEPARATOR: Final = r"\s{0,3}[:=\uff1a]{1,2}\s{0,3}"
_LOOSE_SEPARATOR: Final = r"(?:\s{1,3}is\s{0,3}:?\s{0,3}|\s{1,3})"
_USE_LABEL: Final = r"(?:use|enter|type)\s{1,3}(?:the\s{1,3})?(?:password|pin)(?!\w)"
"""One code is used or entered; "use passwords safely" is about passwords in general."""
_COMMON_WORD: Final = (
    r"(?!(?:protected|required|needed|provided|sent|shared|available|included|optional"
    r"|below|above|upon|will|is|are|was|be|the|a|an|to|for|of|and|or|not|no|in|on|at|by"
    r"|via|with|from|this|that|none|tba|tbd|n/a)(?![\w/]))"
)
_PROSE_WORD: Final = (
    r"(?!(?:codes?|managers?|management|policy|policies|hashing|hash(?:es)?|reuse|resets?"
    r"|strength|entry|entries|fields?|prompts?|protection|security|authentication|logins?"
    r"|storage|cracking|guessing|attacks?|leaks?|breaches|sharing|habits|choices?|rules"
    r"|requirements?|changes?|length|numbers?|pads?|meters?|schemes?|systems?|users?"
    r"|twice|again|instead|first|here|there|now|then|later|too|also|only|once|itself"
    r"|safe|secret|secure|private|wrong|right|correct|incorrect|online|offline)(?![\w/])"
    r"|[a-z]{2,40}(?:ly|ing)(?![\w/]))"
)
"""Words that follow "password" or "PIN" in prose about passwords ("password managers")."""
_NOT_A_QUESTION: Final = r"(?<!\?)(?![ \t]{0,3}\?)"
"""A word that ends a question ("Is the passcode secret?") is asked about, not given."""
_NEXT: Final = (
    r"(?:(?<=[.!?;])(?=\s|\Z)"
    r"|(?=[ \t]{0,3}(?:[.,;:!?]{1,3}[ \t]{0,3})?(?:[\n\r)\]}]|\Z|https?://"
    r"|(?:meeting|webinar|zoom|conference)\s{1,3}(?:id|number)\b|pass[\s-]?codes?\b"
    r"|passwords?\b|pin\b|join\b|dial\b|one[\s-]tap\b|to\s{1,3}(?:join|enter|access)\b"
    r"|when\s{1,3}(?:prompted|asked)\b|if\s{1,3}(?:prompted|asked|needed)\b)))"
)
_LAST_WORD: Final = rf"{_COMMON_WORD}(?=\S{{2}})(?>{_ENCLOSED}|{_WORD}){_NEXT}"
_LOOSE_LAST_WORD: Final = (
    rf"{_COMMON_WORD}{_PROSE_WORD}(?=\S{{2}})(?>{_ENCLOSED}|{_WORD}){_NOT_A_QUESTION}{_NEXT}"
)
_STRONG_CODE: Final = (
    rf"(?P<label>{_LABEL_START.format(latin=_STRONG_LABEL)}{_QUALIFIER}{_SEPARATOR})"
)
_DIGIT_CODES: Final = (
    rf"\b(?P<weak>(?:{_WEAK_LABEL}){_QUALIFIER}{_SEPARATOR}){_HAS_DIGIT}"
    rf"(?=[\d\x20]{{4}}|\S{{4}})(?P<weak_value>{_VALUE})"
    rf"|(?P<loose>{_LABEL_START.format(latin=_STRONG_LABEL + '|' + _PASSWORD_WORDS)}{_QUALIFIER}"
    rf"(?:\s{{1,3}}is\s{{0,3}}:?\s{{0,3}}|{_CJK_IS}|\s{{1,3}})){_HAS_DIGIT}(?P<digits>{_VALUE})"
)
_CODE: Final = re.compile(
    rf"{_STRONG_CODE}(?P<value>{_VALUE})|{_DIGIT_CODES}"
    rf"|\b(?P<weak_word>(?:{_WEAK_LABEL}){_QUALIFIER}{_STRICT_SEPARATOR})(?P<word>{_LAST_WORD})"
    rf"|\b(?P<loose_word>(?:{_STRONG_LABEL}|{_USE_LABEL}){_QUALIFIER}{_LOOSE_SEPARATOR})"
    rf"(?P<loose_value>{_LOOSE_LAST_WORD})",
    re.IGNORECASE,
)
_TITLE_END: Final = r"(?=[ \t]{0,3}(?:[.,;:!?]{1,3}[ \t]{0,3})?(?:[)\]}]|\Z))"
"""The end of a title, or of the brackets a code is given in."""
_TITLE_CODE: Final = re.compile(
    rf"{_STRONG_CODE}{_HAS_DIGIT}(?P<value>{_VALUE})|{_DIGIT_CODES}"
    rf"|\b(?P<title_label>(?:{_STRONG_LABEL}){_QUALIFIER}{_STRICT_SEPARATOR})"
    rf"(?P<title_word>{_COMMON_WORD}{_PROSE_WORD}(?=\S{{2}})(?>{_ENCLOSED}|{_WORD}){_TITLE_END})",
    re.IGNORECASE,
)
"""What ``scrub_title`` takes out: the codes of ``_CODE`` whose value has a digit, and the
one word after "Passcode:" or "Zoom password:" that ends the title."""
_LABEL_GROUPS: Final = ("label", "weak", "loose", "weak_word", "loose_word", "title_label")
_VALUE_GROUPS: Final = ("value", "weak_value", "digits", "word", "loose_value", "title_word")

# --- telephone numbers ---------------------------------------------------------------------

_EXTENSION: Final = r"(?:\s?(?:ext\.?|extension|x)\s?\d{1,6}(?!\d))?"
_DIAL: Final = r"(?:,{1,10}[\d*#]{1,30}){0,8}"
_MEETING_ID: Final = (
    r"\b(?:meeting|webinar|conference|zoom|event)\s{1,3}(?:id|number|no\.?)"
    r"(?:\s{0,3}\(access\s{1,3}code\))?\s{0,3}[:#.]?\s{0,3}\d(?:[\s-]?\d){5,15}#?"
)
_CUED: Final = (
    r"\b(?P<cue>(?:(?:tele?phone|phone|tel|fax|cell\s{1,3}phone|mobile|call|text|sms"
    r"|contact)\b\.?"
    r"(?:\s{0,3}(?:number|no\.?|#))?\s{0,3}[:.]?\s{0,3}(?:(?:us\s{1,3})?(?:at|to|on)\s{1,3})?"
    r"|cell\b\.?(?:\s{0,3}(?:number|no\.?|#)\s{0,3}[:.]?|\s{0,3}:)\s{0,3}))"
    rf"(?P<cued>\+?\(?\d(?:[\s.()-]{{0,2}}\d){{3,17}}{_DIAL}{_EXTENSION})"
)
_AT_LOCAL: Final = (
    r"\b(?P<lead>(?:contact|call|reach|phone|questions?)\b[^.\n\d]{0,40}?\bat\s{1,3})"
    r"(?P<local>[2-9]\d{2}[.-]\d{4})(?![\d-])"
)
"""A campus number of seven digits, after "at" in a sentence about calling someone."""
_NORTH_AMERICAN: Final = (
    r"(?<![\w+/.-])(?:1[\s.-]?)?(?:\(\d{3}\)[\s.-]?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\d)"
    rf"{_DIAL}{_EXTENSION}"
    r"|(?<![\w+/.-])(?:1[\s.-]?)?(?:\([2-9]\d{2}\)[\s.-]?|[2-9]\d{2}[\s.-])[2-9]\d{6}(?!\d)"
    rf"{_DIAL}{_EXTENSION}"
)
_INTERNATIONAL: Final = rf"(?<![\w+])\+\d(?:[\s.()-]{{0,2}}\d){{6,17}}(?!\d){_DIAL}{_EXTENSION}"
_UNSEPARATED: Final = r"(?<![\w+/.=#%-])[2-9]\d{2}[2-9]\d{6}(?!\w|\.\d)"
_CHINA_MOBILE: Final = r"(?<![A-Za-z0-9_+/.=#%-])1[3-9]\d{9}(?![A-Za-z0-9_]|\.\d)"
"""A mobile number of mainland China: eleven digits from 13 to 19, also right after CJK text."""
_PHONE: Final = re.compile(
    rf"(?P<id>{_MEETING_ID})|{_CUED}|{_AT_LOCAL}|{_NORTH_AMERICAN}|{_INTERNATIONAL}"
    rf"|{_UNSEPARATED}|{_CHINA_MOBILE}",
    re.IGNORECASE,
)
_YEARS: Final = re.compile(r"(?:19|20)\d{2}(?:\s?[-/]\s?(?:(?:19|20)\d{2}|\d{2}))?")
_MIN_CUED_DIGITS: Final = 5

# --- markers in names and affiliations -----------------------------------------------------

_MARKERS: Final = (LINK_REMOVED, VALUE_REMOVED, EMAIL_REMOVED, PHONE_REMOVED)
_MARKER_CUE: Final = (
    r"(?:\b(?:e-?mail(?:\s{1,3}address)?|contact|tele?phone|phone|tel|fax|cell(?:\s{1,3}phone)?"
    r"|mobile|pass[\s-]?codes?|pwd|pin|" + _PASSWORD_WORDS + r")\b\.?\s{0,3}[:=#\uff1a]?\s{0,3}"
    r"|(?:" + _CJK_LABEL + r")\s{0,3}[:=#\uff1a]?\s{0,3})?"
)
_MARKER: Final = re.compile(
    _MARKER_CUE + "(?:" + "|".join(re.escape(marker) for marker in _MARKERS) + ")", re.IGNORECASE
)
_DANGLING: Final = re.compile(
    r"[ \t]{0,3}[,;|][ \t]{0,3}(?=[,;|)\]\n]|$)"
    r"|(?:\A|(?<=[(\[\n]))[ \t]{0,3}[,;|][ \t]{0,3}"
)
_EMPTY_BRACKETS: Final = re.compile(r"[ \t]{0,3}(?:\([ \t]{0,3}\)|\[[ \t]{0,3}\]|<[ \t]{0,3}>)")


def _split_tail(text: str) -> tuple[str, str]:
    """``text`` without trailing sentence punctuation, and that punctuation."""
    body = text.rstrip(_TRAILING)
    return body, text[len(body) :]


def _decoded(link: str) -> tuple[str, ...]:
    """``link`` as the links it may wrap: percent-decoded twice, and the Proofpoint form.

    Proofpoint (version 2) writes the wrapped link with ``-XX`` for ``%XX``
    and ``_`` for ``/``.
    """
    forms = [unquote(unquote(link))]
    if _PROOFPOINT_HOST in link.casefold():
        escaped = _PROOFPOINT_ESCAPE.sub(r"%\1", link.replace("_", "/"))
        forms.append(unquote(unquote(escaped)))
    return tuple(forms)


def has_private_address(url: str) -> bool:
    """Whether ``url`` holds a user name, a password or an e-mail address (also wrapped)."""
    try:
        parts = urlsplit(url if "://" in url else f"http://{url}")
        login = parts.username or parts.password
    except ValueError:  # a malformed host or netloc; not a usable address anyway
        return True
    return bool(login) or any(
        _EMAIL.search(form) is not None or _NESTED_LOGIN.search(form) is not None
        for form in _decoded(url)
    )


def _is_credential(piece: str) -> bool:
    name = unquote(unquote(piece)).partition("=")[0]
    return name.strip().casefold() in CREDENTIAL_PARAMETERS


def _without_credentials(component: str) -> str:
    """A query or fragment without its credential parameters; unchanged when it has none."""
    pieces = re.split(r"[&;]", component)
    kept = [piece for piece in pieces if not _is_credential(piece)]
    if len(kept) == len(pieces):
        return component
    return "&".join(piece for piece in kept if piece)


def scrub_link(link: str) -> str:
    """A link of a text without its credentials (see the module docstring)."""
    if has_private_address(link):
        return LINK_REMOVED
    base, hash_mark, fragment = link.partition("#")
    address, question_mark, query = base.partition("?")
    new_query = _without_credentials(query) if question_mark else query
    new_fragment = _without_credentials(fragment) if "=" in fragment else fragment
    stripped = link
    if new_query != query or new_fragment != fragment:
        stripped = address + (f"?{new_query}" if new_query else "")
        stripped += f"#{new_fragment}" if hash_mark and new_fragment else ""
    if any(_NESTED_CREDENTIAL.search(form) is not None for form in _decoded(stripped)):
        return LINK_REMOVED
    return stripped


def is_private_link(url: str) -> bool:
    """Whether ``url`` holds a credential, a login or an e-mail address, also wrapped.

    That is whenever ``scrub_link`` would change it; such an address is
    not used as the address of an event, a speaker or a stream.
    """
    return scrub_link(url) != url


def _link(match: re.Match[str]) -> str:
    body, tail = _split_tail(match[0])
    return scrub_link(body) + tail


def _code(match: re.Match[str]) -> str:
    groups = match.groupdict()
    label = next(groups[name] for name in _LABEL_GROUPS if groups.get(name) is not None)
    value = next(groups[name] for name in _VALUE_GROUPS if groups.get(name))
    _, tail = _split_tail(value)
    return f"{label}{VALUE_REMOVED}{tail}"


def _email(match: re.Match[str]) -> str:
    if match["wrapped"] is not None:
        return ""
    return f"{match['mail_cue'] or ''}{EMAIL_REMOVED}"


def _phone(match: re.Match[str]) -> str:
    if match["id"] is not None:
        return match[0]
    if match["cued"] is not None:
        number = match["cued"]
        digits = sum(char.isdigit() for char in number)
        if digits < _MIN_CUED_DIGITS or _YEARS.fullmatch(number.strip()):
            return match[0]
        return f"{match['cue']}{PHONE_REMOVED}"
    if match["local"] is not None:
        return f"{match['lead']}{PHONE_REMOVED}"
    return PHONE_REMOVED


def _scrub_once(text: str, codes: re.Pattern[str]) -> str:
    linked = _LINK.sub(_link, text)
    coded = codes.sub(_code, linked)
    mailed = _EMAIL.sub(_email, coded)
    return _PHONE.sub(_phone, mailed)


def _scrub(text: str, codes: re.Pattern[str]) -> str:
    current = text
    for _ in range(MAX_PASSES):
        scrubbed = _scrub_once(current, codes)
        if scrubbed == current:
            break
        current = scrubbed
    return current


def scrub_private(text: str) -> str:
    """``text`` without meeting credentials, e-mail addresses and telephone numbers.

    Taking out an address in brackets joins the words around it, which can
    make a label out of them, so the rules are applied again until nothing
    changes, at most ``MAX_PASSES`` times.
    """
    return _scrub(text, _CODE)


def scrub_title(text: str) -> str:
    """``text``, a title, scrubbed like ``scrub_private`` except for most codes without a digit.

    A title names what a talk is about, and talks on security name
    passwords ("Why do people not use password managers?", "Password: a
    history"). A meeting code without a digit is taken out of every other
    field; in a title only links, codes with a digit, e-mail addresses,
    telephone numbers and the one word that ends a title after a label
    such as "Zoom passcode:" are.
    """
    return _scrub(text, _TITLE_CODE)


def strip_markers(text: str) -> str:
    """``text`` (a speaker line, name or affiliation) without the markers of ``scrub_private``.

    A marker goes with the cue in front of it ("Phone: [phone removed]"),
    and separators, brackets and lines that are left empty go too. Text
    without a marker comes back unchanged. The rules run over the text a
    bounded number of times, so the time is linear in its length.
    """
    if not any(marker in text for marker in _MARKERS):
        return text
    current = text
    for _ in range(MAX_PASSES):
        stripped = _MARKER.sub("", current)
        tidied = _DANGLING.sub("", _EMPTY_BRACKETS.sub("", _DANGLING.sub("", stripped)))
        if tidied == current:
            break
        current = tidied
    return "\n".join(line.strip() for line in current.splitlines() if line.strip())
