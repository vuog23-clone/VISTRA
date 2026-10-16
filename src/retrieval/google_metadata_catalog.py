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
        self._places = None

    def places(self):
        if self._places is None:
            self._places = self.store.list_places()
        return self._places

    def location_names(self, location):
        canonical_name = LocationResolver.configured_name(location,self.config)
        names = [canonical_name,location.strip()]
        for setting in ('google_location_aliases','location_aliases'):
            for canonical,aliases in self.config.get(setting,{}).items():
                if LocationResolver._normalize(canonical) == LocationResolver._normalize(canonical_name):
                    names.extend([canonical,*aliases])
        for canonical,country in self.countries.items():
            aliases = [canonical,country['country_code'],*country.get('aliases',[])]
            if LocationResolver._normalize(location) in {LocationResolver._normalize(name) for name in aliases}:
                return aliases
        return list(dict.fromkeys(names))

    def place_in_location(self, place, location):
        return any(LocationResolver.mentioned(name,str(place.get(field) or ''))
                   for name in self.location_names(location) for field in ('address','province'))

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
        names = self.location_names(location)
        ids = [place['place_id'] for place in self.places() if self.place_in_location(place,location)]
        return LocationScope(name=names[0], place_ids=sorted(ids)) if ids else None

    @tracked_tool("PostgreSQL", "Filter Google photo categories")
    def category_place_ids(self, categories, place_ids=None, *, excluded=()):
        wanted = set(checked_categories(categories))
        unwanted = set(checked_categories(list(excluded)))
        allowed = set(place_ids) if place_ids is not None else None
        return sorted(place["place_id"] for place in self.places()
            if (not wanted or metadata_photo_category(place) in wanted)
            and metadata_photo_category(place) not in unwanted
            and (allowed is None or place["place_id"] in allowed))

    @tracked_tool('PostgreSQL','Match explicitly named photo places')
    def named_place_constraints(self, query, *, location=None):
        """Exact catalog name mentions, never fuzzy similarity as identity.

        Return positive IDs (None when unconstrained), excluded IDs and the
        remaining category subject. Unknown named subjects return [] without fallback.
        """
        import re
        import unicodedata
        from src.retrieval.category_filters import CATEGORY_ALIASES, subject_is_excluded
        def words(value):
            # Entity names retain words such as Quán and Island. The geographic
            # normalizer removes administrative prefixes/suffixes and cannot be used here.
            value = unicodedata.normalize('NFKD',value.casefold()).replace('đ','d')
            value = ''.join(char for char in value if not unicodedata.combining(char))
            return ' '.join(re.sub(r'[^\w]+',' ',value.replace('_',' ')).split())
        generic = {words(value) for aliases in CATEGORY_ALIASES.values() for value in aliases}
        generic.update(words(name) for name in self.countries)
        generic.update({'i','me','my','you','your','the','a','an','please','show','find','photos','images',
                        'toi','minh','ban','cho','xem','anh','hinh'})
        if location:
            generic.update(words(name) for name in self.location_names(location))
        text = words(query or '')
        found = []
        for place in self.places():
            name = words(place.get('place_name') or '')
            if not name or name in generic:
                continue
            for match in re.finditer(r'(?<!\w)'+re.escape(name)+r'(?!\w)',text):
                found.append((match.start(),match.end(),place['place_id']))
        found = [(start,end,place_id) for start,end,place_id in found if not any(
            other_start <= start and other_end >= end and (other_start,other_end) != (start,end)
            for other_start,other_end,_ in found)]
        ids = {place_id for start,_,place_id in found if not subject_is_excluded(text[:start])}
        excluded = {place_id for start,_,place_id in found if subject_is_excluded(text[:start])}
        quoted = list(re.finditer(r'"([^"\n]+)"|“([^”\n]+)”',query or ''))
        generic.update(name+'s' for name in list(generic))
        generic.add('beaches')
        strict = any(words(match[1] or match[2]) not in generic and
                     not subject_is_excluded(words(query[:match.start()])) for match in quoted)
        if not ids:
            # A clearly named but unindexed subject must not silently become a
            # similarity search for different venues. Descriptive requests stay broad.
            photo_subject = re.search(r'\b(?:photos?|pictures?|images?)\s+(?:of|showing)\s+(.+)|'
                                      r'\b(?:ảnh|hình ảnh)\s+(?:(?:của|về)\s+)?(.+)',query or '',re.I)
            if photo_subject and not subject_is_excluded(words(query[:photo_subject.start()])):
                subject_name = re.split(r'[,;.!?]|\b(?:in|at|near|around|without|except|ở|tại|gần|không)\b',
                    photo_subject[1] or photo_subject[2],maxsplit=1,flags=re.I)[0]
                ignored = {word for name in generic for word in name.split()}
                ignored.update({'the','some','beautiful','pretty','scenic','stunning','best','quiet','nice','dep','nhung','cac','places','destinations'})
                strict = strict or any(word[0].isupper() and words(word) not in ignored
                    for word in re.findall(r'[^\W\d_]+',subject_name))
        # Words inside a real place name do not determine that place's category.
        subject = text
        for start,end in sorted({(start,end) for start,end,_ in found},reverse=True):
            subject = subject[:start]+' '+subject[end:]
        return (sorted(ids) if ids or strict else None),sorted(excluded),subject if found else query

    @tracked_tool("PostgreSQL", "Read Google place and photo metadata")
    def lookup(self, results):
        return self.store.get_retrieval_records(
            [result.place_id for result in results], [result.point_id for result in results])

    def close(self):
        if self._owns_store and self._store is not None:
            self._store.close()
            self._store = None
