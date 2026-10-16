from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import asdict
from math import isfinite
from pathlib import Path
from threading import RLock
from PIL import Image

from src.schema.models import AtomicRetrievalRoute, RetrievalResult, RetrievalRoute, SearchResponse, SearchResult
from src.router.jev import JevRouter
from src.retrieval.result_enricher import ResultEnricher
from src.resolver.location_resolver import LocationResolver
from src.resolver.image_distance_resolver import ImageDistanceResolver
from src.utils.utils import load_config

from src.retrieval.image_retriever import ImageRetriever
from src.retrieval.text_retriever import TextRetriever
from src.retrieval.category_filters import checked_categories, photo_category_constraints
from src.retrieval.image_filters import image_matches_filters, place_metadata
from src.utils.progress import retrieval_progress
from src.security.guardrails import validate_request, validate_threshold, bounded_int, MAX_LIMIT, MAX_GROUP_SIZE
from src.security.images import load_safe_image


class SearchService:
    """Retrieve guide text alone or guide text with distance-ranked images."""

    def __init__(
        self,
        text_retriever: TextRetriever | None = None,
        image_retriever: ImageRetriever | None = None,
        enricher: ResultEnricher | None = None,
        router: JevRouter | None = None,
        text_embedder_factory=None,
        location_resolver: LocationResolver | None = None,
        metadata_store=None,
        image_distance_resolver=None,
        image_embedder_factory=None,
        sparse_embedder_factory=None,
    ):
        self.config = load_config()
        self._text_retriever = text_retriever
        self._image_retriever = image_retriever
        self._enricher = enricher
        self._router = router
        self._text_embedder_factory = text_embedder_factory
        self._image_embedder_factory = image_embedder_factory
        self._sparse_embedder_factory = sparse_embedder_factory
        self._location_resolver = location_resolver
        self._google_catalog = None
        self._metadata_store = metadata_store
        self._image_distance_resolver = image_distance_resolver or ImageDistanceResolver()
        self._resources = ExitStack()
        self._closed = False

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("SearchService is closed.")


    def _route(
        self,
        query: str | None,
        *,
        has_image: bool,
        override: RetrievalRoute | None,
    ) -> RetrievalRoute:
        if self._router is None:
            self._router = JevRouter()
            self._resources.callback(self._router.close)
        return self._router.route(query, has_image=has_image, override=override)


    def _text(self) -> TextRetriever:
        if self._text_retriever is None:
            from src.retrieval.text_retriever import TextRetriever

            embedder = self._text_embedder_factory() if self._text_embedder_factory is not None else None
            sparse = self._sparse_embedder_factory() if self._sparse_embedder_factory is not None else None
            self._text_retriever = TextRetriever(embedder=embedder, sparse_embedder=sparse)
            self._resources.callback(self._text_retriever.close)
        return self._text_retriever


    def _image(self) -> ImageRetriever:
        if self._image_retriever is None:
            embedder = self._image_embedder_factory() if self._image_embedder_factory is not None else None
            self._image_retriever = ImageRetriever(embedder=embedder)
            self._resources.callback(self._image_retriever.close)
        return self._image_retriever


    def _location(self) -> LocationResolver:
        if self._location_resolver is None:
            self._location_resolver = LocationResolver()
            self._resources.callback(self._location_resolver.close)
        return self._location_resolver

    def _image_location(self):
        if self._google_catalog is None:
            from src.retrieval.google_metadata_catalog import GoogleMetadataCatalog
            self._google_catalog = GoogleMetadataCatalog(store=self._metadata_store, config=self.config)
            self._resources.callback(self._google_catalog.close)
        return self._google_catalog


    def _enrich(self) -> ResultEnricher:
        if self._enricher is None:
            self._enricher = ResultEnricher(catalog=self._image_location())
            self._resources.callback(self._enricher.close)
        return self._enricher


    def _enrich_groups(
        self,
        groups: list[list[RetrievalResult]],
        source: str,
        *, maps_only: bool = False,
    ) -> list[SearchResult]:
        groups = [group for group in groups if group]

        if not groups:
            return []
        
        hits = [hit for group in groups for hit in group]
        if source == "image" and maps_only:
            enriched = ResultEnricher.enrich_image_places_from_payload(hits)
        else:
            enricher = self._enrich()
            enriched = (enricher.enrich_text_results(hits) if source == "text"
                        else enricher.enrich_image_results(hits))
        
        results = []
        offset = 0
        images_by_identity = {(hit.place_id,hit.point_id):hit for hit in enriched} if source == 'image' else {}

        for group in groups:
            evidence = ([images_by_identity[(hit.place_id,hit.point_id)] for hit in group
                if hit.place_id == group[0].place_id and (hit.place_id,hit.point_id) in images_by_identity]
                if source == 'image' else enriched[offset:offset + len(group)])
            offset += len(group)
            if not evidence:
                continue
            results.append(
                SearchResult(
                    **asdict(evidence[0]),
                    evidence=evidence
                )
            )

        return results


    def search(self, query=None, *, image=None, route=None, limit=None, group_size=None,
               score_threshold=None, location=None, visual_query=None, categories=None,
               on_progress=None, maps_only=False):
        self._check_open()
        query, location, visual_query, categories = validate_request(
            query, image=image, route=route, location=location, visual_query=visual_query,
            categories=categories, limit=limit, group_size=group_size,
            score_threshold=score_threshold, maps_only=maps_only)
        # Validate configured defaults as well as caller overrides.
        for key in ("search_limit", "retrieval_limit"):
            bounded_int(self.config[key], key, MAX_LIMIT)
        bounded_int(self.config["retrieval_group_size"], "retrieval_group_size", MAX_GROUP_SIZE)
        validate_threshold(self.config.get("image_search_score_threshold"))
        with ExitStack() as resources:
            if image is not None:
                image = load_safe_image(image)
                resources.callback(image.close)
            return self._search(query, image=image, route=None if route == "auto" else route,
                limit=limit, group_size=group_size, score_threshold=score_threshold,
                location=location, visual_query=visual_query, categories=categories,
                on_progress=on_progress, maps_only=maps_only)

    def _search(
        self,
        query: str | None = None,
        *,
        image: str | Path | Image.Image | None = None,
        route: RetrievalRoute | None = None,
        limit: int | None = None,
        group_size: int | None = None,
        score_threshold: float | None = None,
        location: str | None = None,
        visual_query: str | None = None,
        categories: list[str] | None = None,
        on_progress=None,
        maps_only: bool = False,
    ) -> SearchResponse:
        self._check_open()
        if type(maps_only) is not bool:
            raise TypeError("maps_only must be a boolean.")
        for name, value in (("limit", limit), ("group_size", group_size)):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{name} must be a positive integer.")
        if score_threshold is not None and (isinstance(score_threshold, bool)
                or not isinstance(score_threshold, (int, float)) or not isfinite(score_threshold)):
            raise ValueError("score_threshold must be a finite number.")
        if query is not None and not isinstance(query, str):
            raise TypeError("query must be a string or None.")
        for name, value in (("location", location), ("visual_query", visual_query)):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{name} must be a string or None.")
        if categories is not None:
            categories = checked_categories(categories)
        with retrieval_progress(on_progress):
            selected = self._route(query, has_image=image is not None, override=route)
        if selected == "hybrid":
            return self._search_multimodal(
                query, image=image, limit=limit, group_size=group_size,
                score_threshold=score_threshold, location=location,
                visual_query=visual_query, categories=categories, on_progress=on_progress,
                maps_only=maps_only,
            )
        with retrieval_progress(on_progress):
            response = self._search_atomic(
                query, image=image, route=selected, limit=limit, group_size=group_size,
                score_threshold=score_threshold, location=location,
                visual_query=visual_query, categories=categories, on_progress=on_progress,
                maps_only=maps_only,
            )
            response.retrieval_status = "complete" if response.results else "partial"
            response.branch_status = {selected: response.location_status or (
                "complete" if response.results else "no_results")}
            if on_progress is not None:
                on_progress({"step": "retrieval", "title": "Retrieval completed",
                    "status": "done", "results": len(response.results)})
            return response

    def _search_multimodal(self, query, *, image=None, on_progress=None, **options):
        """Prepare safely, search concurrently, enrich and rerank after both finish."""
        visual_route = "image_image" if image is not None else "image_text"
        responses = {}
        event_lock = RLock()

        def emit(event):
            if on_progress is not None:
                with event_lock:
                    on_progress(event)

        with retrieval_progress(emit if on_progress is not None else None):
            prepared = {}
            # Initialize shared catalogs/resources and filters on the caller thread.
            # Only the independent embedding/vector searches execute concurrently.
            for branch in ("text", visual_route):
                def branch_progress(event, branch=branch):
                    emit({**event, "step": f"{branch}_{event.get('step', 'retrieval')}", "branch": branch})

                prepared[branch] = self._prepare_search(query, image=image, route=branch,
                    on_progress=branch_progress if on_progress is not None else None, **options)
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="hybrid-retrieval") as pool:
                pending = {}
                for branch, (retrieve, _) in prepared.items():
                    emit({"step": f"hybrid_{branch}", "title": f"Run hybrid {branch} retrieval",
                          "status": "running", "branch": branch})
                    pending[branch] = pool.submit(copy_context().run, retrieve or (lambda: None))
                # Metadata enrichment remains sequential: the catalogs are shared.
                groups = {branch: future.result() for branch, future in pending.items()}
            for branch, (_, finish) in prepared.items():
                responses[branch] = finish(groups[branch])
                emit({"step": f"hybrid_{branch}", "title": f"Hybrid {branch} retrieval finished",
                      "status": "done", "branch": branch, "results": len(responses[branch].results)})
            target = responses["text"].location or options.get("location")
            images, ranking = self._image_distance_resolver.rerank(
                responses["text"].results, responses[visual_route].results, location=target)
            responses[visual_route].results = images
            emit({**asdict(ranking), "step": "image_reranking", "title": "Image distance ranking completed",
                  "ranking_status": ranking.status, "status": "done" if ranking.ranked_images else "skipped"})

        text, visual = responses["text"], responses[visual_route]
        response = SearchResponse(
            route="hybrid", results=text.results, related_results=visual.results,
            image_reranking=ranking,
            maps_only=options.get("maps_only", False),
            location=options.get("location"), requested_categories=visual.requested_categories,
            excluded_categories=visual.excluded_categories, requested_place_ids=visual.requested_place_ids,
            excluded_place_ids=visual.excluded_place_ids,
            branch_status={branch: item.location_status or ("complete" if item.results else "no_results")
                           for branch, item in responses.items()},
            retrieval_warnings=[warning for item in responses.values() for warning in item.retrieval_warnings],
        )
        nonempty = sum(bool(item.results) for item in responses.values())
        response.retrieval_status = "complete" if nonempty == 2 else "partial"
        if ranking.status == "skipped_no_reference":
            response.retrieval_warnings.append(
                "Image distance ranking skipped: no matching retrieved destination document has valid coordinates; similarity order retained.")
        elif ranking.unranked_images:
            response.retrieval_warnings.append(
                f"{ranking.unranked_images} image place(s) lack valid coordinates and follow the distance-ranked candidates.")
        if options.get("location"):
            if all(item.location_status == "unresolved" for item in responses.values()):
                response.location_status = "unresolved"
            else:
                response.location = next(item.location for item in responses.values()
                                         if item.location_status != "unresolved")
            for branch, item in responses.items():
                if not item.results:
                    response.retrieval_warnings.append(
                        f"The {branch} branch returned no evidence: {item.location_status or 'no_matches'}.")
        if on_progress is not None:
            on_progress({"step": "retrieval", "title": "Hybrid retrieval completed", "status": "done",
                         "results": len(response.results) + len(response.related_results),
                         "branch_status": response.branch_status})
        return response

    def _search_atomic(self, query, **options):
        retrieve, finish = self._prepare_search(query, **options)
        return finish(retrieve() if retrieve else None)

    def _prepare_search(
        self,
        query: str | None = None,
        *,
        image: str | Path | Image.Image | None = None,
        route: AtomicRetrievalRoute | None = None,
        limit: int | None = None,
        group_size: int | None = None,
        score_threshold: float | None = None,
        location: str | None = None,
        visual_query: str | None = None,
        categories: list[str] | None = None,
        on_progress=None,
        page_ids: list[str] | None = None,
        maps_only: bool = False,
    ):
        """Prepare vector retrieval and metadata enrichment for a direct search."""
        self._check_open()
        selected = route
        if selected not in ("text", "image_text", "image_image"):
            raise ValueError("An atomic retrieval route is required.")
        if selected != "image_image" and (not isinstance(query, str) or not query.strip()):
            raise ValueError("Text retrieval requires a nonblank query.")
        default_limit = self.config["retrieval_limit"] if selected == "text" else self.config["search_limit"]
        limit = limit if limit is not None else default_limit
        # None lets text retrieval retain its full document budget on one page;
        # explicit group-size overrides still use the request's smaller cap.
        default_group_size = None if selected == "text" else self.config["retrieval_group_size"]
        group_size = group_size if group_size is not None else default_group_size
        location = location.strip() if location and location.strip() else None
        explicit_categories = bool(categories)
        inferred, excluded = photo_category_constraints(query) if selected != 'text' else ([],[])
        categories = checked_categories(categories or inferred) if selected != "text" else []
        response = SearchResponse(route=selected, location=location, requested_categories=categories,
            excluded_categories=excluded,maps_only=maps_only)
        location_resolver = (self._location() if selected == "text" else self._image_location()) if location or categories or excluded else None
        scope = location_resolver.resolve(location) if location else None
        if location and scope is None:
            response.location_status = "unresolved"
            if on_progress is not None:
                on_progress({"step": "scope", "title": "Location could not be resolved", "detail": "No travel index query will be performed."})
            return None, lambda _: response
        if on_progress is not None:
            detail = f"{scope.name}: {len(scope.place_ids)} eligible places; {len(scope.page_ids)} Wikivoyage pages." if scope else "No destination constraint."
            on_progress({"step": "scope", "title": "Location scope resolved before retrieval", "detail": detail})
        if scope is not None:
            response.location = scope.name
            if not (scope.page_ids if selected == "text" else scope.place_ids):
                response.location_status = "no_indexed_text" if selected == "text" else "no_indexed_images"
                return None, lambda _: response

        allowed_places = scope.place_ids if scope else None
        if selected != 'text' and not maps_only:
            named, excluded_names, subject = self._image_location().named_place_constraints(query,location=location)
            inferred, excluded = photo_category_constraints(subject)
            categories = categories if explicit_categories else inferred
            response.requested_categories, response.excluded_categories = categories,excluded
            response.requested_place_ids = named
            response.excluded_place_ids = excluded_names
            if excluded_names:
                allowed_places = sorted(set(allowed_places if allowed_places is not None else
                    [place['place_id'] for place in self._image_location().places()])-set(excluded_names))
            if named is not None:
                allowed_places = sorted(set(named).intersection(allowed_places)) if allowed_places is not None else named
                if not allowed_places:
                    response.location_status = 'no_matching_place'
                    response.retrieval_warnings.append('No indexed photo place matches the named subject within the requested location.')
                    return None,lambda _:response
        if categories or excluded:
            allowed_places = self._image_location().category_place_ids(categories, allowed_places,excluded=excluded)
            if on_progress is not None:
                on_progress({"step": "scope", "title": "Photo categories checked",
                    "detail": f"Required categories: {', '.join(categories)}; excluded: {', '.join(excluded)}; {len(allowed_places)} eligible places before similarity ranking."})
            if not allowed_places:
                response.location_status = "no_matching_category"
                return None, lambda _: response

        if selected == "text":
            allowed_pages = scope.page_ids if scope else page_ids
            if scope is not None and page_ids is not None:
                allowed_pages = sorted(set(scope.page_ids) & set(page_ids))
            retriever = self._text()
            retrieve = lambda: retriever.search_grouped(query, limit=limit, group_size=group_size,
                score_threshold=score_threshold, page_ids=allowed_pages)
        else:
            allowed_pages = None
            if on_progress is not None:
                on_progress({"step": "image_dataset", "title": "Image retrieval dataset selected",
                    "detail": f"Dataset: Google; collection: {self.config['google_images_collection']}; "
                              + ("metadata: Qdrant payload." if maps_only else "metadata: PostgreSQL.")})
            retriever = self._image()
            threshold = score_threshold if score_threshold is not None else self.config.get("image_search_score_threshold")
            method = retriever.search_grouped if selected == "image_image" else retriever.search_text_grouped
            value = image if selected == "image_image" else visual_query or query
            retrieve = lambda: method(value, limit=limit, group_size=group_size,
                score_threshold=threshold, place_ids=allowed_places)

        def finish(groups):
            results = self._enrich_groups(groups or [], "text" if selected == "text" else "image", maps_only=maps_only)
            if selected != 'text':
                raw_count = sum(len(group) for group in groups or [])
                filtered = []
                for result in results:
                    valid, seen_points = [],set()
                    for hit in [result,*result.evidence]:
                        key = (hit.place_id,hit.point_id)
                        if key in seen_points:
                            continue
                        seen_points.add(key)
                        if image_matches_filters(hit,categories=categories,excluded=excluded,
                                place_ids=allowed_places,excluded_place_ids=response.excluded_place_ids,require_photo=not maps_only) and (
                                not location or self._image_location().place_in_location(place_metadata(hit),location)):
                            valid.append(hit)
                    if not valid:
                        continue
                    from dataclasses import replace
                    kept = replace(result,**{key:getattr(valid[0],key) for key in (
                        'place_id','point_id','score','source','payload','entity_id','name','latitude','longitude',
                        'url','category','province','address','image','metadata','missing_metadata')}, evidence=[])
                    kept.evidence = [hit for hit in valid[1:] if hit.place_id == kept.place_id]
                    if not any(hit.place_id == kept.place_id for hit in filtered):
                        filtered.append(kept)
                results = filtered
                if raw_count and not results:
                    response.retrieval_warnings.append('Photo candidates failed identity, category or location checks, or lacked a bound photo. No unrelated replacements were added.')
            if on_progress is not None:
                on_progress({"step": "metadata", "title": "Retrieval metadata loaded",
                    "detail": ("Wikivoyage page and coordinate metadata." if selected == "text"
                        else "Google place metadata and Maps links from Qdrant payloads." if maps_only
                        else "Google locations and photo URLs from PostgreSQL."),
                    "status": "done", "places": len(results)})
            # Defensive scope check after enrichment. The actual vector queries
            # have already been constrained by allowed_pages / allowed_places.
            if (allowed_pages if selected == "text" else allowed_places) is not None:
                allowed = set(allowed_pages if selected == "text" else allowed_places)
                results = [result for result in results if result.place_id in allowed]
                for result in results:
                    result.evidence = [hit for hit in result.evidence if hit.place_id == result.place_id]
                if not results:
                    response.location_status = "no_matches"
                    if categories:
                        response.location_status = "no_matching_category"
                    elif selected != "text":
                        if scope is not None and retriever.client.count(
                            collection_name=retriever.collection_name,
                            count_filter=ImageRetriever._location_filter(scope.place_ids),
                            exact=True,
                        ).count == 0:
                            response.location_status = "no_indexed_images"
            response.results = results
            return response

        return retrieve, finish


    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._resources.close()

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
