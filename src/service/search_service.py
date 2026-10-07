from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
from math import isfinite
from pathlib import Path
from PIL import Image

from src.schema.models import HybridSearchResponse, RetrievalResult, RetrievalRoute, SearchResponse, SearchResult
from src.router.jev import JevRouter
from src.retrieval.result_enricher import ResultEnricher
from src.resolver.location_resolver import LocationResolver
from src.utils.utils import load_config

from src.ranking.hybrid_ranker import HybridRanker
from src.resolver.destination_resolver import DestinationResolver
from src.retrieval.image_retriever import ImageRetriever
from src.retrieval.text_retriever import TextRetriever


class SearchService:
    def __init__(
        self,
        text_retriever: TextRetriever | None = None,
        image_retriever: ImageRetriever | None = None,
        enricher: ResultEnricher | None = None,
        destination_resolver: DestinationResolver | None = None,
        hybrid_ranker: HybridRanker | None = None,
        router: JevRouter | None = None,
        text_embedder_factory=None,
        location_resolver: LocationResolver | None = None,
    ):
        self.config = load_config()
        self._text_retriever = text_retriever
        self._image_retriever = image_retriever
        self._enricher = enricher
        self._destination_resolver = destination_resolver
        self._hybrid_ranker = hybrid_ranker
        self._router = router
        self._text_embedder_factory = text_embedder_factory
        self._location_resolver = location_resolver
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
            self._text_retriever = TextRetriever(embedder=embedder)
            self._resources.callback(self._text_retriever.close)
        return self._text_retriever


    def _image(self) -> ImageRetriever:
        if self._image_retriever is None:
            from src.retrieval.image_retriever import ImageRetriever

            self._image_retriever = ImageRetriever()
            self._resources.callback(self._image_retriever.close)
        return self._image_retriever


    def _location(self) -> LocationResolver:
        if self._location_resolver is None:
            self._location_resolver = LocationResolver()
            self._resources.callback(self._location_resolver.close)
        return self._location_resolver


    def _enrich(self) -> ResultEnricher:
        if self._enricher is None:
            self._enricher = ResultEnricher()
            self._resources.callback(self._enricher.close)
        return self._enricher


    def _enrich_groups(
        self,
        groups: list[list[RetrievalResult]],
        source: str
    ) -> list[SearchResult]:
        groups = [group for group in groups if group]

        if not groups:
            return []
        
        hits = [hit for group in groups for hit in group]
        enricher = self._enrich()

        enriched = (
            enricher.enrich_text_results(hits) if source == "text"
            else enricher.enrich_image_results(hits)
        )
        
        results = []
        offset = 0

        for group in groups:
            evidence = enriched[offset:offset + len(group)]
            offset += len(group)
            results.append(
                SearchResult(
                    **asdict(evidence[0]),
                    evidence=evidence
                )
            )

        return results

    def _resolve_results(
        self,
        results: list[SearchResult],
        enabled: bool
    ) -> list[SearchResult]:
        if enabled and results:
            if self._destination_resolver is None:
                from src.resolver.destination_resolver import DestinationResolver

                self._destination_resolver = DestinationResolver()

            resolutions = self._destination_resolver.resolve_results(results)
            for result, resolution in zip(results, resolutions, strict=True):
                result.destination = resolution

        return results

    def search_text(
        self,
        query: str,
        limit: int | None = None,
        group_size: int | None = None,
        score_threshold: float | None = None,
        *,
        resolve_destinations: bool = False,
        page_ids: list[str] | None = None,
    ) -> list[SearchResult]:
        self._check_open()
        limit = limit if limit is not None else self.config["search_limit"]
        group_size = group_size if group_size is not None else self.config["retrieval_group_size"]
        groups = self._text().search_grouped(
            query,
            limit=limit,
            group_size=group_size,
            score_threshold=score_threshold,
            page_ids=page_ids,
        )
        return self._resolve_results(self._enrich_groups(groups, "text"), resolve_destinations)

    def search_image(
        self,
        query: str | Path | Image.Image,
        limit: int | None = None,
        group_size: int | None = None,
        score_threshold: float | None = None,
        *,
        resolve_destinations: bool = False,
        place_ids: list[str] | None = None,
    ) -> list[SearchResult]:
        self._check_open()
        limit = limit if limit is not None else self.config["search_limit"]
        group_size = group_size if group_size is not None else self.config["retrieval_group_size"]
        if place_ids == []:
            return []
        score_threshold = score_threshold if score_threshold is not None else self.config.get("image_search_score_threshold")
        groups = self._image().search_grouped(
            query,
            limit=limit,
            group_size=group_size,
            score_threshold=score_threshold,
            place_ids=place_ids,
        )
        return self._resolve_results(self._enrich_groups(groups, "image"), resolve_destinations)

    def search(
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
        on_progress=None,
    ) -> SearchResponse:
        self._check_open()
        selected = self._route(query, has_image=image is not None, override=route)
        if on_progress is not None:
            method = "Wikivoyage dense + BM25 search" if selected == "text" else "SigLIP photo similarity search"
            on_progress({"step": "route", "title": "Retrieval route selected", "detail": f"{selected}: {method}.", "route": selected})
        location = location.strip() if location and location.strip() else None
        response = SearchResponse(route=selected, location=location)
        scope = self._location().resolve(location) if location else None
        if location and scope is None:
            response.location_status = "unresolved"
            if on_progress is not None:
                on_progress({"step": "scope", "title": "Location could not be resolved", "detail": "No travel index query will be performed."})
            return response
        if on_progress is not None:
            detail = f"{scope.name}: {len(scope.place_ids)} eligible places; {len(scope.page_ids)} Wikivoyage pages." if scope else "No destination constraint."
            on_progress({"step": "scope", "title": "Location scope checked", "detail": detail})
        if scope is not None:
            response.location = scope.name
            if not (scope.page_ids if selected == "text" else scope.place_ids):
                response.location_status = "no_indexed_text" if selected == "text" else "no_indexed_images"
                return response

        if selected == "text":
            results = self.search_text(
                query, limit, group_size, score_threshold,
                page_ids=scope.page_ids if scope else None,
            )

        elif selected == "image_image":
            results = self.search_image(
                image, limit, group_size, score_threshold,
                place_ids=scope.place_ids if scope else None,
            )

        else:
            results = self.search_image_text(
                visual_query or query, limit, group_size, score_threshold,
                place_ids=scope.place_ids if scope else None,
            )

        if scope is not None:
            allowed = set(scope.page_ids if selected == "text" else scope.place_ids)
            results = [result for result in results if result.place_id in allowed]
            for result in results:
                result.evidence = [hit for hit in result.evidence if hit.place_id == result.place_id]
            if not results:
                response.location_status = "no_matches"
                if selected != "text":
                    retriever = self._image()
                    if retriever.client.count(
                        collection_name=retriever.collection_name,
                        count_filter=ImageRetriever._location_filter(scope.place_ids),
                        exact=True,
                    ).count == 0:
                        response.location_status = "no_indexed_images"
        response.results = results
        return response

    def search_follow_up(
        self,
        query: str,
        previous: SearchResponse,
        limit: int | None = None,
    ) -> SearchResponse:
        """Keep the selected entities; retrieve additional text only within known pages."""
        self._check_open()
        response = deepcopy(previous)
        response.related_results = []
        response.follow_up_status = ""
        if previous.route == "text":
            page_ids = [result.place_id for result in response.results if result.source == "text"]
        else:
            if self.config["destination_mappings_path"] is not None:
                self._resolve_results(response.results, True)
            page_ids = [result.destination.page_id for result in response.results
                        if result.destination is not None
                        and result.destination.method == "curated_mapping"
                        and result.destination.source_entity_id == result.entity_id
                        and result.destination.page_id is not None
                        and result.destination.destination_id == f"wikivoyage:{result.destination.page_id}"]
        if not page_ids:
            response.follow_up_status = "metadata_only"
            return response
        additional = self.search_text(query, limit=limit, page_ids=page_ids)
        if previous.route == "text":
            by_id = {result.entity_id: result for result in additional}
            for index, result in enumerate(response.results):
                if result.entity_id in by_id:
                    refreshed = by_id[result.entity_id]
                    refreshed.result_number = result.result_number
                    response.results[index] = refreshed
        else:
            response.related_results = additional
        if not additional:
            response.follow_up_status = "no_additional_information"
        return response

    def search_image_text(
        self,
        query: str,
        limit: int | None = None,
        group_size: int | None = None,
        score_threshold: float | None = None,
        *,
        place_ids: list[str] | None = None,
    ) -> list[SearchResult]:
        self._check_open()
        limit = limit if limit is not None else self.config["search_limit"]
        group_size = group_size if group_size is not None else self.config["retrieval_group_size"]
        if place_ids == []:
            return []
        score_threshold = score_threshold if score_threshold is not None else self.config.get("image_search_score_threshold")
        groups = self._image().search_text_grouped(
            query,
            limit=limit,
            group_size=group_size,
            score_threshold=score_threshold,
            place_ids=place_ids,
        )
        return self._enrich_groups(groups, "image")

    def search_hybrid(
        self,
        query: str | None = None,
        *,
        image: str | Path | Image.Image | None = None,
        limit: int | None = None,
        candidate_limit: int | None = None,
        group_size: int | None = None,
        text_score_threshold: float | None = None,
        image_score_threshold: float | None = None,
    ) -> HybridSearchResponse:
        self._check_open()
        limit = limit if limit is not None else self.config["search_limit"]
        group_size = group_size if group_size is not None else self.config["retrieval_group_size"]

        if candidate_limit is None:
            candidate_limit = max(self.config["candidate_min_limit"],limit * self.config["candidate_multiplier"])

        text_results = self.search_text(
            query, limit=candidate_limit, group_size=group_size,
            score_threshold=text_score_threshold, resolve_destinations=True,
        ) if query is not None else []

        image_results = self.search_image(
            image, limit=candidate_limit, group_size=group_size,
            score_threshold=image_score_threshold, resolve_destinations=True,
        ) if image is not None else []
        
        if self._hybrid_ranker is None:
            from src.ranking.hybrid_ranker import HybridRanker

            self._hybrid_ranker = HybridRanker()
        return self._hybrid_ranker.rank(text_results, image_results, limit=limit)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._resources.close()

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def main() -> None:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Search and enrich distinct VISTRA places or destinations.")
    parser.add_argument("--text", help="Travel search query")
    parser.add_argument("--image", type=Path, help="Inspiration image path")
    parser.add_argument("--location", help="Restrict results to a metadata-backed destination")
    parser.add_argument("--visual-query", help="Scenery description without the destination name")
    parser.add_argument("--route", choices=["text", "image_text", "image_image"], help="Override automatic routing")
    parser.add_argument("--hybrid", action="store_true", help="Use optional destination mapping and fusion")
    parser.add_argument("--candidate-limit", type=int, help="Entities to retrieve per source in hybrid search")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--group-size", type=int)
    parser.add_argument("--score-threshold", type=float)
    parser.add_argument("--resolve-destinations", action="store_true", help="Attach destination mappings without changing retrieval order")
    args = parser.parse_args()
    if args.text is None and args.image is None:
        parser.error("Provide --text, --image, or both.")
    if args.candidate_limit is not None and not args.hybrid:
        parser.error("--candidate-limit requires --hybrid.")
    if args.hybrid and args.route is not None:
        parser.error("--route and --hybrid cannot be combined.")
    if args.hybrid and (args.location or args.visual_query):
        parser.error("Use normal routed search for location-constrained retrieval.")
    sys.stdout.reconfigure(encoding="utf-8")
    with SearchService() as service:
        if args.hybrid:
            response = service.search_hybrid(
                args.text, image=args.image, limit=args.limit,
                candidate_limit=args.candidate_limit, group_size=args.group_size,
                text_score_threshold=args.score_threshold, image_score_threshold=args.score_threshold,
            )
            output = asdict(response)
        else:
            response = service.search(
                args.text, image=args.image, route=args.route, limit=args.limit,
                group_size=args.group_size, score_threshold=args.score_threshold,
                location=args.location, visual_query=args.visual_query,
            )
            if args.resolve_destinations:
                service._resolve_results(response.results, True)
            output = asdict(response)
        print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    import sys
    from time import perf_counter

    if len(sys.argv) > 1:
        main()
    else:
        # Edit these values, then run: python -m src.service.search_service
        route = None  # None = automatic; or "text", "image_text", "image_image".
        query = "Where should I visit in Phu Quoc?"
        image_path = None
        limit = 5
        group_size = 2

        if image_path is not None and not Path(image_path).is_file():
            raise FileNotFoundError(f"Example image not found: {image_path}")
        sys.stdout.reconfigure(encoding="utf-8")
        print("Starting search service example | Routing query...", flush=True)
        started = perf_counter()
        with SearchService() as service:
            selected = service._route(query, has_image=image_path is not None, override=route)
            print(f"Route: {selected} | Loading model, retrieving matches, and enriching...", flush=True)
            response = service.search(
                query, image=image_path, route=selected, limit=limit, group_size=group_size,
            )
        print(f"Completed in {perf_counter() - started:.2f}s")
        print(f"\nRoute: {response.route} | Results: {len(response.results)}")
        for index, hit in enumerate(response.results, start=1):
            print(f"\n{index}. {hit.name} | {hit.entity_id}")
            print(f"Source: {hit.source} | Retrieval score: {hit.score:.4f}")
            print(f"Coordinates: {hit.latitude}, {hit.longitude} | Supporting hits: {len(hit.evidence)}")
            print(f"URL: {hit.url}")
            if hit.image:
                print(f"Image path: {hit.image.get('image_path')}")
            if hit.source == "text":
                print(f"Excerpt: {str(hit.payload.get('text', ''))[:350]}")
