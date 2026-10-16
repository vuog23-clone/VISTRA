"""Explicit photo categories narrow the metadata scope before vector ranking."""

import re
import unicodedata


CATEGORY_ALIASES = {
    "beach": {"beach", "bai bien"}, "museum": {"museum", "bao tang"},
    "park": {"park", "garden", "cong vien", "vuon hoa"},
    "restaurant": {"restaurant", "nha hang", "quan an"},
    "cafe": {"cafe", "coffee shop", "ca phe", "quan cafe"},
    "market": {"market", "marketplace", "khu cho"}, "viewpoint": {"viewpoint", "diem ngam canh"},
    "attraction": {"attraction", "diem tham quan"},
    "hotel": {"hotel", "khach san"}, "hostel": {"hostel"}, "resort": {"resort", "khu nghi duong"},
    "lake": {"lake", "ho nuoc"}, "mountain": {"mountain", "nui"},
    "waterfall": {"waterfall", "thac nuoc", "thac"},
}


def metadata_photo_category(metadata):
    """Normalize dataset category labels for the current photo filters."""
    value = str(metadata.get("category") or "").casefold().strip()
    aliases = {
        "cafe": {"cafe", "coffee shop"},
        "restaurant": {"restaurant", "bistro", "brunch restaurant"},
        "market": {"market", "marketplace"},
        "attraction": {"attraction", "tourist attraction", "historical landmark",
                       "theme park", "amusement park", "zoo"},
        "beach": {"beach", "public beach"}, "park": {"park", "garden", "botanical garden"},
        "mountain": {"mountain", "mountain peak"},
    }
    if value.endswith(" restaurant"):
        return "restaurant"
    if value == "museum" or value.endswith(" museum"):
        return "museum"
    return next((key for key, values in aliases.items() if value in values), value or None)


def explicit_photo_categories(query):
    return photo_category_constraints(query)[0]


def photo_category_constraints(query):
    """Explicit subjects/exclusions only; applied after the visual route is chosen.

    Nearby categories describe geography, not the photo subject. Quoted place
    names are resolved by identity rather than guessed from words in the name.
    """
    original = re.sub(r'\bchợ\b','market',(query or '').casefold())
    text = unicodedata.normalize('NFKD',original).replace('đ','d')
    text = ''.join(char for char in text if not unicodedata.combining(char)).replace('\u2019',"'")
    forms = {form:category for category,aliases in CATEGORY_ALIASES.items() for alias in aliases
             for form in {alias, 'beaches' if alias == 'beach' else alias+'s' if ' ' not in alias else alias}}
    text = re.sub(r'"([^"\n]+)"|“([^”\n]+)”',
        lambda match:(match[1] or match[2]) if (match[1] or match[2]) in forms else ' ',text)
    # "Cho tôi xem" is a request verb, not a request for photos of markets.
    text = re.sub(r'\bcho\s+(?:(?:toi|minh|chung toi)\s+)?xem\b','show',text)
    relation = re.search(r'\b(?:near|nearby|around|in|at|along|o|tai|gan)\b',text)
    if relation:
        tail = text[relation.start():]
        restart = re.search(r'[,;.!?]|\b(?:but|instead|show|find|include|not|no|without|exclud(?:e|ing)|except|khong|tru)\b',tail)
        text = text[:relation.start()]+(' ; '+tail[restart.start():] if restart else '')
    subjects = '|'.join(re.escape(form) for form in sorted(forms,key=len,reverse=True))
    tokens = re.compile(r'(?P<reset>[;.!?]|\b(?:but|instead|show|find|suggest|recommend|include|nhung|tim|goi y)\b)'
        r"|(?P<negative>\b(?:(?:do not|don't|dont|don t)\s+(?:show|include)|not(?!\s+only)|no|without|exclud(?:e|ing)|except|khong(?!\s+chi)|ngoai tru|tru)\b)"
        r'|(?P<category>\b(?:'+subjects+r')\b)')
    included, excluded, negative = [], [], False
    for token in tokens.finditer(text):
        if token.lastgroup == 'reset':
            negative = False
        elif token.lastgroup == 'negative':
            negative = True
        else:
            destination = excluded if negative else included
            category = forms[token.group()]
            if category not in destination:
                destination.append(category)
    return included, excluded


def subject_is_excluded(prefix):
    """Track explicit negative/positive clauses before a catalog name mention."""
    text = re.sub(r"\b(?:do not|don't|dont|don t)\s+(?:show|include)\b",'without',prefix)
    negative = False
    for cue in re.finditer(r'\b(?P<negative>not(?!\s+only)|no|without|exclude|excluding|except|khong(?!\s+chi)|tru)\b'
                           r'|\b(?P<positive>show|find|include|suggest|recommend|but|instead|xem|tim|nhung)\b',text):
        negative = cue.lastgroup == 'negative'
    return negative


def checked_categories(values):
    if values is not None and (not isinstance(values, (list, tuple)) or len(values) > len(CATEGORY_ALIASES)
                               or any(not isinstance(value, str) for value in values)):
        from src.security.guardrails import UnsafeInput
        raise UnsafeInput("invalid_categories", "categories must be a short list of supported category names.")
    categories = list(dict.fromkeys(values or []))
    if set(categories) - CATEGORY_ALIASES.keys():
        from src.security.guardrails import UnsafeInput
        raise UnsafeInput("invalid_categories", "Unsupported photo category.")
    return categories


