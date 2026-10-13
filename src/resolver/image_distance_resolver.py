"""Rank retrieved image places by distance to a retrieved guide destination."""

from math import asin, cos, isfinite, radians, sin, sqrt

from src.resolver.location_resolver import LocationResolver
from src.schema.models import ImageDistanceRanking
from src.utils.retrieval_activity import tracked_tool


class ImageDistanceResolver:
    @staticmethod
    def coordinates(result):
        lat, lon = result.latitude, result.longitude
        if (type(lat) not in (int, float) or type(lon) not in (int, float)
                or not isfinite(lat) or not isfinite(lon)
                or not -90 <= lat <= 90 or not -180 <= lon <= 180):
            return None
        return float(lat), float(lon)

    @staticmethod
    def distance_km(origin, destination):
        """Great-circle distance on a sphere with mean Earth radius 6371.0088 km."""
        lat1, lon1, lat2, lon2 = map(radians, (*origin, *destination))
        value = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
        return 6371.0088 * 2 * asin(sqrt(max(0.0, min(1.0, value))))

    @tracked_tool("Image distance resolver", "Rank image places by destination distance")
    def rerank(self, documents, images, *, location=None):
        """Return sorted copies; keep raw similarity scores and document order.

        A named scope must match a retrieved document, never an unrelated page.
        Without a scope, use the first ranked document with valid coordinates.
        Equal distances and unknown distances retain their retrieval order.
        Coordinates describe the indexed image's place, not camera GPS.
        """
        from dataclasses import replace

        images = [replace(image, distance_km=None,
                          evidence=[replace(hit, distance_km=None) for hit in image.evidence])
                  for image in images]
        if not images:
            return images, ImageDistanceRanking(status="skipped_no_images")
        target = LocationResolver._normalize(location) if location else None
        reference = next((document for document in documents
            if document.source == "text" and self.coordinates(document) is not None
            and (target is None or (document.name and LocationResolver._normalize(document.name) == target))), None)
        if reference is None:
            return images, ImageDistanceRanking(status="skipped_no_reference", unranked_images=len(images))
        origin = self.coordinates(reference)
        ranked = 0
        for image in images:
            coordinates = self.coordinates(image) if image.source == "image" else None
            if coordinates is not None:
                image.distance_km = self.distance_km(origin, coordinates)
                ranked += 1
            for hit in image.evidence:
                coordinates = self.coordinates(hit) if hit.source == "image" else None
                if coordinates is not None:
                    hit.distance_km = self.distance_km(origin, coordinates)
        images.sort(key=lambda image: image.distance_km if image.distance_km is not None else float("inf"))
        return images, ImageDistanceRanking(
            status="complete" if ranked == len(images) else "partial",
            reference_entity_id=reference.entity_id, reference_name=reference.name,
            reference_latitude=origin[0], reference_longitude=origin[1],
            ranked_images=ranked, unranked_images=len(images) - ranked)
