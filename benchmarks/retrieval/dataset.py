"""Define labels from saved metadata before issuing any retrieval query.

No imports from production category/scope filters: these predicates form the
independent metadata oracle. They do not establish visual scene relevance.
"""

import re
import unicodedata

CITIES = {
    'Hanoi':{'vi':'Hà Nội','aliases':['Hanoi','Ha Noi']},
    'Ho Chi Minh City':{'vi':'Thành phố Hồ Chí Minh','aliases':['Ho Chi Minh','Saigon','Sai Gon']},
    'Da Nang':{'vi':'Đà Nẵng','aliases':['Da Nang']},
    'Hue':{'vi':'Huế','aliases':['Hue']},
    'Da Lat':{'vi':'Đà Lạt','aliases':['Da Lat','Dalat']},
}
CATEGORIES = {'cafe':('cafes','quán cà phê'),'restaurant':('restaurants','nhà hàng'),
    'museum':('museums','bảo tàng'),'park':('parks','công viên'),'hotel':('hotels','khách sạn')}


def words(value):
    value = unicodedata.normalize('NFKD',(value or '').casefold()).replace('đ','d')
    return ' '.join(re.sub(r'[^\w]+',' ',''.join(c for c in value if not unicodedata.combining(c))).split())


def in_city(place,city):
    text = ' '+words(str(place.get('province') or '')+' '+str(place.get('address') or ''))+' '
    return any(' '+words(alias)+' ' in text for alias in CITIES[city]['aliases'])


def category(place):
    value = words(place.get('category'))
    if value in {'cafe','coffee shop'}:
        return 'cafe'
    if value in {'restaurant','bistro','brunch restaurant'} or value.endswith(' restaurant'):
        return 'restaurant'
    if value=='museum' or value.endswith(' museum'):
        return 'museum'
    if value in {'park','garden','botanical garden'}:
        return 'park'
    if value=='hotel':
        return 'hotel'
    return None


def build(places,photos,indexed_pairs,chunks):
    photo_ids = {p['place_id'] for p in photos if (p['place_id'],str(p['image_id'])) in indexed_pairs}
    eligible = [p for p in places if p['place_id'] in photo_ids]
    cases,labels = [],{}
    family = 0
    def append(kind,city,messages,relevant,**extra):
        nonlocal family
        family += 1
        for language,message in zip(('en','vi'),messages):
            cid = f'RI1-{family:03d}-{language}'
            cases.append({'case_id':cid,'family_id':f'RI1-{family:03d}','kind':kind,
                'language':language,'city':city,'query':message,**extra})
            labels[cid] = sorted(set(relevant))
    for city,details in CITIES.items():
        for cat,(en,vi) in CATEGORIES.items():
            ids = [p['place_id'] for p in eligible if in_city(p,city) and category(p)==cat]
            append('category',city,(f'Show photos of {en} in {city}.',f'Cho tôi xem ảnh {vi} ở {details["vi"]}.'),ids,category=cat)
    for city,details in CITIES.items():
        for cat in ('cafe','restaurant','hotel'):
            candidates = sorted([p for p in eligible if in_city(p,city) and category(p)==cat],key=lambda p:p['place_id'])
            if not candidates:
                raise ValueError(f'No metadata-supported known-place case for {city}/{cat}.')
            target = candidates[0]
            ids = [p['place_id'] for p in eligible if in_city(p,city) and words(p['place_name'])==words(target['place_name'])]
            append('known_place',city,(f'Show photos of "{target["place_name"]}" in {city}.',
                f'Cho tôi xem ảnh của "{target["place_name"]}" ở {details["vi"]}.'),ids,target_name=target['place_name'])
    for city,details in CITIES.items():
        for section,en,vi in [('Get in',f'How can I travel to {city}?',f'Tôi có thể đến {details["vi"]} bằng phương tiện nào?'),
                             ('Get around',f'How can I get around {city}?',f'Tôi có thể di chuyển trong {details["vi"]} như thế nào?')]:
            ids = [c['chunk_id'] for c in chunks if c['title']==city and c.get('section')==section]
            if not ids:
                raise ValueError(f'Missing guide section {city}/{section}.')
            append('guide_section',city,(en,vi),ids,section=section)
    if len(cases)!=100 or len({c['family_id'] for c in cases})!=50:
        raise ValueError('The suite must contain 50 families / 100 language cases.')
    return cases,labels
