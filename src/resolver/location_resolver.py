from pathlib import Path
import re
import unicodedata

from src.schema.models import LocationScope
from src.utils.utils import load_config, read_json, read_jsonl, resolve_path
from src.utils.retrieval_activity import tracked_tool
from src.security.guardrails import validate_text


class LocationResolver:
    def __init__(
        self,
        mappings_path: str | Path | None = None,
        pages_path: str | Path | None = None,
    ):
        config = load_config()
        mappings_path = mappings_path or config.get("location_mappings_path")
        self.mappings = read_json(resolve_path(mappings_path)) if mappings_path else {}
        country_path = config.get("country_scopes_path")
        self.countries = read_json(resolve_path(country_path)) if country_path else {}
        # Explicit aliases retain geographic qualifiers without guessing that
        # every comma-separated suffix can safely be removed.
        aliases_by_name = {}
        for setting in ("location_aliases", "google_location_aliases"):
            for name, aliases in config.get(setting, {}).items():
                aliases_by_name.setdefault(name, []).extend(aliases)
        for name, aliases in aliases_by_name.items():
            record = self.mappings.setdefault(name, {})
            record["aliases"] = list(dict.fromkeys([*record.get("aliases", []), *aliases]))
        self.pages_path = resolve_path(pages_path, config["wikivoyage_pages_path"])
        self._pages: list[dict] | None = None
        self._page_names: dict[str, set[str]] | None = None


    @staticmethod
    def _normalize_words(value: str) -> str:
        value = unicodedata.normalize("NFKD", value.casefold().replace("đ", "d"))
        value = "".join(char for char in value if not unicodedata.combining(char))
        value = " ".join(re.sub(r"[^\w]+", " ", value.replace("_", " ")).split())
        value = re.sub(r"^(?:thanh pho|thi xa|tinh|huyen|quan|phuong|xa|tp|city of|province of) ", "", value)
        value = re.sub(r" (?:city|province|island)$", "", value)
        return value


    @staticmethod
    def _normalize(value: str) -> str:
        return LocationResolver._normalize_words(value).replace(" ", "")


    @staticmethod
    def mentioned(location: str, message: str) -> bool:
        """Match a named destination with word boundaries and accent tolerance."""
        words = LocationResolver._normalize_words(location).split()
        if not words:
            return False
        # Permit Phu Quoc / Phuquoc, but never Paris inside "comparison".
        pattern = r"(?<!\w)" + r"\s*".join(re.escape(word) for word in words) + r"(?!\w)"
        return re.search(pattern, LocationResolver._normalize_words(message)) is not None


    def _load_pages(self) -> None:
        if self._pages is None:
            self._pages = list(read_jsonl(self.pages_path)) if self.pages_path.is_file() else []


    def canonical_name(self, location: str) -> str | None:
        """Recognize catalog titles/aliases without opening a metadata/vector DB."""
        location = validate_text(location, "location", max_chars=160)
        key = self._normalize(location)
        if not key:
            return None
        names = {
            name for name, record in self.mappings.items()
            if key in {self._normalize(alias) for alias in [name, *record.get("aliases", [])]}
        }
        names.update(
            name for name, record in self.countries.items()
            if key in {self._normalize(alias) for alias in [name, record["country_code"], *record.get("aliases", [])]}
        )
        if names:
            return next(iter(names)) if len(names) == 1 else None
        self._load_pages()
        if self._page_names is None:
            page_names = {}
            for page in self._pages:
                page_names.setdefault(self._normalize(page["title"]), set()).add(page["title"])
            self._page_names = page_names
        names = self._page_names.get(key, set())
        return next(iter(names)) if len(names) == 1 else None


    @tracked_tool("Location resolver", "Resolve verified destination scope")
    def resolve(self, location: str) -> LocationScope | None:
        location = validate_text(location, "location", max_chars=160)
        self._load_pages()
        name, mapping = location.strip(), {}
        key = self._normalize(name)
        if not key:
            return None
        for candidate, record in self.mappings.items():
            if key in {self._normalize(alias) for alias in [candidate, *record.get("aliases", [])]}:
                name, mapping = candidate, record
                break
        aliases = {self._normalize(alias) for alias in [name, *mapping.get("aliases", [])]}
        country = next((record for candidate, record in self.countries.items()
            if key in {self._normalize(alias) for alias in [candidate, record["country_code"], *record.get("aliases", [])]}), None)
        if country:
            aliases |= {self._normalize(alias) for candidate, record in self.countries.items() if record is country
                        for alias in [candidate, record["country_code"], *record.get("aliases", [])]}
        page_ids = [str(page["page_id"]) for page in self._pages if self._normalize(page["title"]) in aliases]
        if not mapping and not page_ids:
            return None
        return LocationScope(name=name, page_ids=page_ids)


    def close(self) -> None:
        pass
