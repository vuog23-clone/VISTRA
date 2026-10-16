"""Identity and category checks shared by retrieval, RAG and gallery output."""

from src.data.preprocessing.google_maps_photos import checked_photo_url
from src.retrieval.category_filters import metadata_photo_category


def value(hit, key, default=None):
    return hit.get(key,default) if isinstance(hit,dict) else getattr(hit,key,default)


def place_metadata(hit):
    return (value(hit,'metadata',{}) or {}).get('place') or value(hit,'payload',{}) or {}


def place_identity_matches(hit):
    place_id = value(hit,'place_id')
    if value(hit,'source') != 'image' or not isinstance(place_id,str) or not place_id:
        return False
    if value(hit,'entity_id') != 'google:'+place_id:
        return False
    place, payload = place_metadata(hit), value(hit,'payload',{}) or {}
    if place.get('place_id') != place_id or payload.get('place_id',place_id) != place_id:
        return False
    # Never attach one entity's place label or geography to another entity.
    for field,source_field in (('name','place_name'),('address','address'),('province','province')):
        if value(hit,field) and place.get(source_field) and value(hit,field) != place[source_field]:
            return False
    if value(hit,'category') and place.get('category') and metadata_photo_category(
            {'category':value(hit,'category')}) != metadata_photo_category(place):
        return False
    return True


def bound_photo_url(hit):
    """Only the photo row joined to this place/point may supply its URL."""
    if not place_identity_matches(hit):
        return None
    photo = value(hit,'image') or {}
    place_id, point_id = value(hit,'place_id'), value(hit,'point_id')
    if photo.get('place_id') != place_id:
        return None
    if point_id is not None and str(photo.get('image_id')) != str(point_id):
        return None
    stored = (value(hit,'metadata',{}) or {}).get('image')
    if stored and (stored.get('place_id') != place_id or stored.get('image_url') != photo.get('image_url')
                   or point_id is not None and str(stored.get('image_id')) != str(point_id)):
        return None
    try:
        return checked_photo_url(photo.get('image_url'))
    except (ValueError,TypeError):
        return None


def image_matches_filters(hit, *, categories=(), excluded=(), place_ids=None, excluded_place_ids=(), require_photo=False):
    if not place_identity_matches(hit):
        return False
    if place_ids is not None and value(hit,'place_id') not in place_ids:
        return False
    if value(hit,'place_id') in excluded_place_ids:
        return False
    place = place_metadata(hit)
    category = metadata_photo_category({'category':place.get('category') or value(hit,'category')})
    if categories and category not in categories or category in excluded:
        return False
    return not require_photo or bound_photo_url(hit) is not None
