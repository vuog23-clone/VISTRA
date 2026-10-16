"""English/Vietnamese cues for messages returned without an answer LLM."""
import re
import unicodedata


_VI_WORDS = set("toi minh ban hay muon can cho ve gi nao nhung cac khong bao nhieu tim kiem goi y tai o voi va cua den tu dia diem an uong gioi thieu thong tin mo gio ngay dem doi thanh tong cong ngan sach cap nhat tang giam len xuong moi nguoi".split())
_EN_WORDS = set("i you please tell what where when which how show find recommend suggest compare about the in on is are can does for with to from and near opening hours price places photos similar".split())


def query_language(query):
    """Prefer explicit language requests, then grammar cues over place-name accents.

    This small heuristic is for deterministic messages. The answer LLM and its
    reviewer infer language from the original question using their prompts.
    Ambiguous short queries default to English.
    """
    normalized = "".join(char for char in unicodedata.normalize("NFD", query.casefold())
                         if not unicodedata.combining(char)).replace("đ", "d")
    requests = re.findall(
        r"\b(?:answer|respond|reply|tra loi)\b[^.!?\n]{0,35}?\b(english|vietnamese|tieng anh|tieng viet)\b",
        normalized,
    )
    if requests:
        return "vi" if requests[-1] in {"vietnamese", "tieng viet"} else "en"
    words = re.findall(r"[a-z]+", normalized)
    vietnamese = sum(word in _VI_WORDS for word in words)
    english = sum(word in _EN_WORDS for word in words)
    return "vi" if vietnamese > english else "en"


def localized_message(query, english, vietnamese):
    return vietnamese if query_language(query) == "vi" else english
