import psycopg

from src.database.postgres_google_metadata_store import PostgresGoogleMetadataStore
from src.utils.retrieval_activity import tracked_tool
from src.resolver.location_resolver import LocationResolver
from src.retrieval.category_filters import checked_categories, metadata_photo_category
from src.schema.models import LocationScope
from src.utils.errors import ToolUnavailable
from src.utils.utils import load_config, read_json, resolve_path
from src.security.guardrails import validate_text


class GoogleMetadataCatalog:
    def __init__(
            self, 
            *, 
            store=None, 
            config=None
        ):
        self.config = load_config() if config is None else config
        self._store = store
        self._owns_store = store is None
        country_path = self.config.get("country_scopes_path")
        self.countries = read_json(resolve_path(country_path)) if country_path else {}

    @property
    def store(self):
        if self._store is None:
            try:
                self._store = PostgresGoogleMetadataStore()
            except (psycopg.Error, ValueError) as error:
                raise ToolUnavailable("Google PostgreSQL metadata is unavailable; check the service and connection settings.") from error
        return self._store

    @tracked_tool("PostgreSQL", "Resolve Google destination scope")
    def resolve(self, location):
        location = validate_text(location, "location", max_chars=160)
        key = LocationResolver._normalize(location)
        if not key:
            return None
        names = [location.strip()]
        for canonical, aliases in self.config.get("google_location_aliases", {}).items():
            if key in {LocationResolver._normalize(name) for name in [canonical, *aliases]}:
                names = [canonical, *aliases]
                break
        for canonical, country in self.countries.items():
            aliases = [canonical, country["country_code"], *country.get("aliases", [])]
            if key in {LocationResolver._normalize(name) for name in aliases}:
                names = aliases
                break
        ids = [place["place_id"] for place in self.store.list_places()
            if any(LocationResolver.mentioned(name, str(place.get(field) or ""))
                for name in names for field in ("address", "province"))]
        return LocationScope(name=names[0], place_ids=sorted(ids)) if ids else None

    @tracked_tool("PostgreSQL", "Filter Google photo categories")
    def category_place_ids(self, categories, place_ids=None):
        wanted = set(checked_categories(categories))
        allowed = set(place_ids) if place_ids is not None else None
        return sorted(place["place_id"] for place in self.store.list_places()
            if metadata_photo_category(place) in wanted
            and (allowed is None or place["place_id"] in allowed))

    @tracked_tool("PostgreSQL", "Read Google place and photo metadata")
    def lookup(self, results):
        return self.store.get_retrieval_records(
            [result.place_id for result in results], [result.point_id for result in results])

    def close(self):
        if self._owns_store and self._store is not None:
            self._store.close()
            self._store = None
