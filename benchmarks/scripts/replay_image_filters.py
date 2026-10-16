"""Live identity/category diagnostic against existing metadata and image vectors.

Hybrid is selected explicitly to isolate filtering from intent classification.
No generation, photo pixel downloads or conversation database writes occur.
"""

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import argparse
import json
from pathlib import Path
import sys
from time import perf_counter

from api.runtime import RetrievalRuntime
from frontend.media import build_place_cards
from src.generation.rag_generation import build_rag_context
from src.retrieval.category_filters import metadata_photo_category
from src.retrieval.image_filters import image_matches_filters, place_metadata
from src.service.search_service import SearchService
from src.utils.redaction import CredentialRedactor
from src.utils.utils import PROJECT_ROOT


CASES = [
    ('VB1-RETR-106','Suggest beaches in Phu Quoc, with supporting guides.','Phu Quoc',['beach'],False),
    ('VB1-RETR-119','Show Phu Quoc beach photos and retain place details if photos fail.','Phu Quoc',['beach'],False),
    ('VB1-RETR-115','Recommend beach destinations throughout Vietnam.','Vietnam',['beach'],False),
    ('da-nang-exclusions-en','Show beach photos in Da Nang, not restaurants or hotels.','Da Nang',['beach'],False),
    ('da-nang-exclusions-vi','Cho tôi xem ảnh bãi biển ở Đà Nẵng, không phải nhà hàng hay khách sạn.','Da Nang',['beach'],False),
    ('same-name-wrong-region','Show photos of "Biển Mỹ Khê Quảng Ngãi" in Da Nang.','Da Nang',None,True),
    ('unindexed-subject','Show photos of "Unindexed Diagnostic Museum" in Hanoi.','Hanoi',None,True),
    ('VB1-ROUTE-114','Show photos of places in Phu Quoc.','Phu Quoc',[],False),
]


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required to access the existing retrieval services.')
    output = args.output.resolve()
    if not output.is_relative_to((PROJECT_ROOT/'benchmarks'/'runs').resolve()):
        parser.error('Output must be inside benchmarks/runs/.')
    output.mkdir(parents=True,exist_ok=True)
    files = [PROJECT_ROOT/'main.py',PROJECT_ROOT/'config.yaml',*sorted((PROJECT_ROOT/'src').rglob('*.py')),PROJECT_ROOT/'frontend/media.py']
    report = {'scope':__doc__,'started_at_utc':datetime.now(timezone.utc).isoformat(),
        'code_sha256':{str(path.relative_to(PROJECT_ROOT)):sha256(path.read_bytes()).hexdigest() for path in files},'cases':[]}
    runtime, redactor = RetrievalRuntime(), CredentialRedactor()
    try:
        cases = list(CASES)
        with SearchService() as probe:
            catalog = probe._image_location()
            candidate = next((place for place in catalog.places() if metadata_photo_category(place)=='beach'
                and catalog.place_in_location(place,'Vietnam') and catalog.store.get_images_for_place(place['place_id'])),None)
            if candidate:
                cases.append(('indexed-named-beach','Show photos of "'+candidate['place_name']+'" in Vietnam.',
                              'Vietnam',None,False))
        for name,query,location,categories,must_be_empty in cases:
            start = perf_counter()
            row = {'case':name,'query':query,'location':location}
            print(name+': starting',flush=True)
            try:
                with SearchService(text_embedder_factory=lambda:runtime.text,image_embedder_factory=lambda:runtime.image,
                                   sparse_embedder_factory=lambda:runtime.sparse) as service:
                    response = service.search(query,location=location,route='hybrid',limit=20)
                    catalog = service._image_location()
                    hits = response.related_results
                    context = build_rag_context(response)
                    exported = asdict(response)
                    cards = build_place_cards({'retrieval':exported,'context':context})
                    checks = {
                        'category_contract':categories is None or response.requested_categories==categories,
                        'identities_and_categories':all(image_matches_filters(hit,categories=response.requested_categories,
                            excluded=response.excluded_categories,place_ids=response.requested_place_ids,
                            excluded_place_ids=response.excluded_place_ids,require_photo=True) for hit in hits),
                        'geography':all(catalog.place_in_location(place_metadata(hit),location) for hit in hits),
                        'expected_empty_subject':not must_be_empty or len(hits)==0,
                        'gallery_identity':{card['place_id'] for card in cards}=={hit.place_id for hit in hits},
                    }
                    if categories is not None:
                        checks['generic_subject_not_bound_to_a_name'] = response.requested_place_ids is None
                    if name=='indexed-named-beach':
                        checks['indexed_subject_returned'] = bool(hits)
                    row.update(passed=all(checks.values()),checks=checks,photo_places=len(hits),cards=len(cards),
                        categories=sorted({metadata_photo_category(place_metadata(hit)) for hit in hits}),
                        retrieval=exported,context=context,gallery=cards)
            except Exception as error:
                row.update(passed=False,error={'type':type(error).__name__,'message':str(error)})
            row['elapsed_seconds'] = round(perf_counter()-start,3)
            row = redactor.data(row)
            report['cases'].append({key:row[key] for key in ('case','passed','elapsed_seconds','photo_places','categories') if key in row})
            (output/(name+'.json')).write_text(json.dumps(row,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(json.dumps(report['cases'][-1]),flush=True)
    finally:
        runtime.close()
    report.update(completed_at_utc=datetime.now(timezone.utc).isoformat(),
                  passed=sum(row['passed'] for row in report['cases']),cases_run=len(report['cases']))
    (output/'run.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return 0 if report['passed']==report['cases_run'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
