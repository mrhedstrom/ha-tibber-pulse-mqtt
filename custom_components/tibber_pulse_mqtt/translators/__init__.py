from . import en as _en
from . import sv as _sv
from . import no as _no
from . import de as _de
from . import nl as _nl
from . import da as _da

_LANG = {
    "en": _en.dictionary,
    "sv": _sv.dictionary,
    "no": _no.dictionary,
    "de": _de.dictionary,
    "nl": _nl.dictionary,
    "da": _da.dictionary
}

def load_translation(lang: str) -> dict:
    return _LANG.get(lang, _LANG["en"])