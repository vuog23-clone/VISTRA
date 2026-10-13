"""Let Jev choose the query destination from locally recognized place names."""

import os
import re
import unicodedata

import httpx
from dotenv import load_dotenv

from src.prompts.location import JEV_LOCATION_PROMPT
from src.resolver.location_resolver import LocationResolver
from src.schema.location import LocationIntent
from src.utils.language import localized_message
from src.utils.redaction import CredentialRedactor
from src.utils.utils import PROJECT_ROOT, load_config, read_jsonl, resolve_path
from src.security.guardrails import validate_text
from src.security.network import post_provider_json


_COMMON_PLACE_WORDS = {
    "a", "am", "an", "and", "are", "as", "at", "be", "but", "by", "can",
    "do", "go", "he", "if", "in", "is", "it", "la", "may", "me", "my",
    "no", "not", "of", "on", "or", "so", "the", "to", "us", "we",
}


class JevLocationExtractor:
    provider = "jev"

    def __init__(self, *, config=None, api_key=None, client=None, catalog_names=None):
        self.config = load_config() if config is None else config
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        self.api_key = api_key if api_key is not None else (
            os.getenv("JEV_API_KEY") or os.getenv("TYPESAFE_API_KEY"))
        self.client = client
        self._owns_client = client is None
        self._names = catalog_names

    def _catalog(self):
        if self._names is None:
            resolver = LocationResolver()
            resolver._load_pages()
            # Travel topics/disambiguation pages (Beaches, Hotels, Springfield,
            # Atlantis, ...) are not geographic destinations. When available,
            # use the processed coordinate catalog to distinguish those pages.
            geo_setting = self.config.get("wikivoyage_geo_tags_path")
            geo_path = resolve_path(geo_setting) if geo_setting else None
            geo_ids = ({str(row["page_id"]) for row in read_jsonl(geo_path)}
                       if geo_path is not None and geo_path.is_file() else None)
            names = {page["title"]: [] for page in resolver._pages
                     if geo_ids is None or str(page["page_id"]) in geo_ids}
            for name, record in resolver.mappings.items():
                names.setdefault(name, []).extend(record.get("aliases", []))
            for name, record in resolver.countries.items():
                names.setdefault(name, []).extend([record["country_code"], *record.get("aliases", [])])
                for province in record.get("province_labels", []):
                    names.setdefault(province, [])
            self._names = names
        return self._names

    @staticmethod
    def _mentioned_as_place(label, query):
        if not LocationResolver.mentioned(label, query):
            return False
        words = LocationResolver._normalize_words(label)
        if words not in _COMMON_PLACE_WORDS:
            return True
        # Accent folding must not turn ordinary "are" into the town Åre.
        # Keep actual accented mentions, uppercase abbreviations and explicitly
        # located names ("in Are, Sweden"), while excluding generic prose.
        literal = unicodedata.normalize("NFKC", label).casefold()
        message = unicodedata.normalize("NFKC", query)
        if literal != words and re.search(r"(?<!\w)" + re.escape(literal) + r"(?!\w)", message.casefold()):
            return True
        if label.isupper() and re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", message):
            return True
        return bool(re.search(r"\b(?:in|at|visit|visiting|to|from|o|tai|den)\s+" + re.escape(words) + r"\b",
                              LocationResolver._normalize_words(query)))

    def candidates(self, query):
        query = validate_text(query, check_intent=True)
        # Keep the catalog's configured canonical aliases (e.g. Hà Nội → Hanoi).
        matches = {}
        normalized = LocationResolver._normalize_words(query).replace(" ", "")
        for name, aliases in self._catalog().items():
            labels = [name, *aliases]
            if any(LocationResolver._normalize(label) in normalized and
                   self._mentioned_as_place(label, query) for label in labels):
                key = LocationResolver._normalize(name)
                matches.setdefault(key, name)
        candidates = sorted(matches.values())
        if len(candidates) > 252:
            raise ValueError("Too many location candidates. Narrow the query or supply --location.")
        return candidates

    def extract_location(self, query):
        query = validate_text(query, check_intent=True)
        if not self.api_key:
            raise ValueError("Set JEV_API_KEY (or TYPESAFE_API_KEY) in .env for location recognition. "
                             "Use --location PLACE or --no-auto-location to skip it.")
        candidates = [validate_text(name, "catalog candidate", max_chars=160)
                      for name in self.candidates(query)]
        choices = {f"place_{index}": name for index, name in enumerate(candidates)}
        criteria = {
            "no_location": "No geographic destination: unscoped image similarity (including 'Tìm nơi giống ảnh này'), generic beaches/photos, or places only as origins/exclusions/analogies/supplied text. Translation/grammar/creative writing belong here unless travel retrieval is ALSO requested.",
            "ambiguous": "Several separate destinations, uncertain namesakes, near me, or an unresolved there/this place reference (including 'What attractions are there?'). Clarification is required.",
            "unknown_destination": "A destination is requested but none of the catalog candidates identifies it. Do not choose a similar place.",
            **{key: f"The user requests travel/search evidence for the proper geographic destination {name}. It is not a generic travel topic/common word, origin, excluded PLACE, analogy or writing/translation content. Ignoring an attached photo does not exclude a named destination." for key, name in choices.items()},
        }
        if self.client is None:
            self.client = httpx.Client(timeout=self.config["jev_timeout_seconds"], follow_redirects=False, trust_env=False)
        value, _ = post_provider_json(self.client, self.config["jev_api_url"], timeout=self.config["jev_timeout_seconds"],
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.config["jev_model"],
                  "state": {"query": CredentialRedactor().text(query), "location_candidates": candidates},
                  "questions": {"location": {"type": "choice", "instructions": JEV_LOCATION_PROMPT,
                                               "criteria": criteria}}})
        answers = value.get("answers") if isinstance(value, dict) else None
        answer = answers.get("location") if isinstance(answers, dict) else None
        choice = answer.get("choice") if isinstance(answer, dict) else None
        if not isinstance(choice, str) or choice not in criteria:
            raise ValueError("Jev returned an invalid location decision. Retry or supply --location.")
        if choice in choices:
            return LocationIntent(status="destination", location=choices[choice], candidates=[], clarification=None)
        if choice == "no_location":
            return LocationIntent(status="no_location", location=None, candidates=[], clarification=None)
        clarification = (localized_message(query,
            "Which destination should I search? Please provide its city/province and country.",
            "Bạn muốn tìm ở địa điểm nào? Vui lòng nêu thành phố/tỉnh và quốc gia.")
            if choice == "ambiguous" else localized_message(query,
            "I could not recognize the requested destination in the catalog. Please clarify its name or supply `--location`.",
            "Không nhận diện được địa điểm trong danh mục. Vui lòng làm rõ tên hoặc dùng `--location`."))
        return LocationIntent(status="ambiguous", location=None, candidates=candidates[:5], clarification=clarification)

    def close(self):
        if self._owns_client and self.client is not None:
            self.client.close()
            self.client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
