"""Read-only actual-index diagnostics. This is not synthetic v1 fixture replay."""

import argparse
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
import random
import sys
from time import perf_counter

from benchmarks.retrieval.dataset import CITIES,build,in_city,category
from benchmarks.retrieval.metrics import ranking,unique
from benchmarks.measurement.engine import ROOT,sources,hashes,dependencies


def digest(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,
        default=lambda obj:obj.model_dump(mode='json')).encode('utf-8')).hexdigest()


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def scroll(client,collection,query_filter=None):
    offset = None
    while True:
        points,offset = client.scroll(collection_name=collection,scroll_filter=query_filter,
            offset=offset,limit=256,with_payload=True,with_vectors=True)
        yield from points
        if offset is None:
            return


def inventory(client,config,pages):
    from qdrant_client import models
    image_leaves,text_leaves,chunks = [],[],[]
    for point in scroll(client,config['google_images_collection']):
        image_leaves.append({'point_id':str(point.id),'place_id':point.payload.get('place_id'),
            'payload_sha256':digest(point.payload),'vectors_sha256':digest(point.vector)})
    ids = [str(p['page_id']) for p in pages]
    query_filter = models.Filter(should=[models.FieldCondition(key='page_id',match=models.MatchAny(any=ids)),
        models.FieldCondition(key='page_id',match=models.MatchAny(any=[int(x) for x in ids]))])
    for point in scroll(client,config['wikivoyage_collection'],query_filter):
        chunks.append(point.payload)
        text_leaves.append({'point_id':str(point.id),'chunk_id':point.payload.get('chunk_id'),
            'payload_sha256':digest(point.payload),'vectors_sha256':digest(point.vector)})
    return {'image_points':sorted(image_leaves,key=lambda r:r['point_id']),
            'scoped_text_points':sorted(text_leaves,key=lambda r:r['point_id'])},chunks


def metadata(store):
    places = sorted(store.list_places(),key=lambda p:p['place_id'])
    photos = store.connection.execute('SELECT image_id::text AS image_id,place_id,image_url FROM google_images ORDER BY image_id').fetchall()
    return {'places':places,'photos':photos}


def average(rows,key):
    values = [r[key] for r in rows if r.get(key) is not None]
    return sum(values)/len(values) if values else None


def summarize(out,cases,rows):
    summary = {'protocol':'vistra-actual-index-retrieval-1','catalog_cases':len(cases),'executed_cases':len(rows),
        'case_errors':sum('error' in r for r in rows),'by_kind':{},'by_language':{},
        'pixel_relevance_scored':False,'routing_scored':False,'llm_generation_used':False}
    for kind in ('category','known_place','guide_section'):
        group = [r for r in rows if r['kind']==kind]
        summary['by_kind'][kind] = {'cases':len(group),'errors':sum('error' in r for r in group)}
        for stage in ('raw','final'):
            metrics = [r[stage] for r in group]
            summary['by_kind'][kind][stage] = {key:average(metrics,key) for key in
                ('precision_at_20','precision_of_returned_at_20','recall_at_20','recall_at_20_ceiling','ndcg_at_10','hit_at_5','reciprocal_rank_at_20')}
        summary['by_kind'][kind]['zero_result_cases'] = sum(r['final']['returned']==0 for r in group)
        summary['by_kind'][kind]['wrong_city_results'] = sum(r.get('wrong_city_results',0) for r in group)
        summary['by_kind'][kind]['wrong_category_results'] = sum(r.get('wrong_category_results',0) for r in group)
        summary['by_kind'][kind]['wrong_identity_results'] = sum(r.get('wrong_identity_results',0) for r in group)
    for language in ('en','vi'):
        group = [r for r in rows if r['language']==language]
        summary['by_language'][language] = {'cases':len(group),'errors':sum('error' in r for r in group),
            'category_final_metadata_precision':average([r['final'] for r in group if r['kind']=='category'],'precision_of_returned_at_20'),
            'guide_section_ndcg_at_10':average([r['final'] for r in group if r['kind']=='guide_section'],'ndcg_at_10')}
    guides = [r for r in rows if r['kind']=='guide_section']
    summary['guide_context_section_hit_cases'] = sum(r.get('context_section_hit',False) for r in guides)
    summary['guide_context_section_hit_denominator'] = len(guides)
    summary['card_wrong_city_count'] = sum(r.get('card_wrong_city_count',0) for r in rows)
    summary['card_wrong_category_count'] = sum(r.get('card_wrong_category_count',0) for r in rows)
    summary['photo_binding_violations'] = sum(r.get('photo_binding_violations',0) for r in rows)
    write(out/'summary.json',summary)
    lines = ['# Actual-index retrieval results','',
        f"Executed **{len(rows)}/{len(cases)} bilingual cases**, with **{summary['case_errors']} case errors**, using VISTRA’s real Voyage/BM25 text retriever, SigLIP text-to-image retriever, Qdrant search, metadata enrichment, filters and context/card assembly.",
        '', 'This is a new actual-index diagnostic cohort, **not a replay of the fictional VB1 retrieval cases**. The old 600-case first-pass report remains unchanged. Its unsupported retrieval row means unmeasured, not failed.',
        '', '| Cohort | Cases | Raw nDCG@10 | Final nDCG@10 | Final precision among returned results | Final Recall@20 |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    def fmt(value):
        return f'{value:.3f}' if value is not None else 'N/A'
    for kind,data in summary['by_kind'].items():
        lines.append(f"| {kind} | {data['cases']} | {fmt(data['raw']['ndcg_at_10'])} | {fmt(data['final']['ndcg_at_10'])} | {fmt(data['final']['precision_of_returned_at_20'])} | {fmt(data['final']['recall_at_20'])} |")
    item = summary['by_kind']['known_place']
    lines += ['', f"Known-place Hit@5: raw **{fmt(item['raw']['hit_at_5'])}**, final **{fmt(item['final']['hit_at_5'])}**. MRR@20: raw **{fmt(item['raw']['reciprocal_rank_at_20'])}**, final **{fmt(item['final']['reciprocal_rank_at_20'])}**.",
        '', f"Requested guide section reached generation context in **{summary['guide_context_section_hit_cases']}/{len(guides)}** cases.",
        '', f"Photo-card wrong-city results: **{summary['card_wrong_city_count']}**; wrong-category results: **{summary['card_wrong_category_count']}**.",
        '', '## What these measures establish','',
        '- Category relevance is independently labeled from city/category metadata, including museum/restaurant subtypes. Known-place relevance uses exact normalized indexed names within the declared city. It does not establish photo appearance, beauty, image identity from pixels, or suitability for dietary/accessibility requirements.',
        '- Guide relevance uses the indexed page and Get in/Get around section labels as a proxy. It is not independently reviewed semantic relevance for every sentence.',
        '- Raw photo ranking uses real SigLIP vectors with only an independently specified city scope. Final search adds the application’s category/name constraints, enrichment, defensive filtering and distance reranking. High final precision can therefore reflect correct filtering rather than a strong raw embedding ranker.',
        '- precision_at_20 uses denominator 20. The table reports precision among actually returned distinct results, separately from Recall@20, result counts and empty-result cases. An empty positive query scores zero. Recall ceilings are recorded because a category may contain more than 20 relevant places.',
        '- Routes and city options are explicitly supplied to isolate retrieval. Jev interpretation/routing, conversational rewrites, LLM generation, image uploads, photo download availability and visual-scene relevance are not evaluated.',
        '- Cases and labels were defined before querying from a read-only metadata/index inventory. Labels are metadata-derived and have not been independently human adjudicated. These are authored diagnostics, not a random generalization sample.',
        '- The existing live index was queried read-only. Before/after vector and payload fingerprints cover all image points and the five scoped guide pages. They detect changes but are not a replayable vector export.',
        '- Weights are loaded once and reused; startup time is recorded separately. No application/model/prompt setting was changed and no OpenAI/Jev generation calls are made.',
        '', '## Files','', '`cases.json` and `qrels.json` contain the declared inputs/labels; `metadata.json`, `guide_chunks.json` and index inventories record evidence provenance. `results.jsonl` and `cases/` retain raw ranks, final search results, context, cards, timings and errors. `verification.json` records source/data consistency checks.']
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return summary


def main():
    from src.utils.utils import load_config,resolve_path,read_jsonl
    from src.database.postgres_google_metadata_store import PostgresGoogleMetadataStore
    from src.database.qdrant_connection import open_qdrant
    from qdrant_client import QdrantClient
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    if not args.execute:
        parser.error('--execute authorizes read-only local model/index evaluation.')
    out=args.output.resolve()
    if not out.is_relative_to((ROOT/'benchmarks/runs').resolve()) or out.exists() and any(out.iterdir()):
        parser.error('Use a new directory inside benchmarks/runs.')
    out.mkdir(parents=True,exist_ok=True)
    (out/'cases').mkdir()
    config=load_config()
    application=sources()
    harness=hashes([*Path(__file__).parent.glob('*.py'),ROOT/'benchmarks/scripts/evaluate_retrieval.py'])
    manifest={'protocol':'vistra-actual-index-retrieval-1','started_at':datetime.now(timezone.utc).isoformat(),
        'setting':'live-index metadata relevance diagnostic','source_hashes':application,'harness_hashes':harness,
        'config':config,'dependencies':dependencies(),'order_seed':16,'repetitions':1,'models':{
            name:config[name] for name in ('text_embedding_model','image_embedding_model','sparse_embedding_model')},
        'model_cache_revisions':{name:[p.name for p in (resolve_path(config['cache_folder'])/('models--'+config[name].replace('/','--'))/'snapshots').glob('*')]
            for name in ('text_embedding_model','image_embedding_model','sparse_embedding_model')}}
    write(out/'run.json',manifest)
    with ExitStack() as resources:
        store=resources.enter_context(PostgresGoogleMetadataStore())
        client=open_qdrant(resolve_path(config['qdrant_path']),config,QdrantClient)
        resources.callback(client.close)
        pages=[p for p in read_jsonl(resolve_path(config['wikivoyage_pages_path'])) if p['title'] in CITIES]
        print('Snapshotting metadata and actual index fingerprints...',flush=True)
        meta=metadata(store)
        before,chunks=inventory(client,config,pages)
        write(out/'metadata.json',meta);write(out/'guide_chunks.json',chunks);write(out/'index_before.json',before)
        cases,labels=build(meta['places'],meta['photos'],{(p['place_id'],p['point_id']) for p in before['image_points']},chunks)
        write(out/'cases.json',cases);write(out/'qrels.json',labels)
        manifest['dataset_sha256']={'metadata':digest(meta),'chunks':digest(chunks),'cases':digest(cases),'qrels':digest(labels),'index':digest(before)}
        write(out/'run.json',manifest)
        print('Loading the real cached embedding models...',flush=True)
        start=perf_counter()
        from src.embedding.text_embedder import TextEmbedder
        from src.embedding.sparse_embedder import SparseEmbedder
        from src.embedding.image_embedder import ImageEmbedder
        from src.retrieval.text_retriever import TextRetriever
        from src.retrieval.image_retriever import ImageRetriever
        from src.service.search_service import SearchService
        from src.generation.rag_generation import build_rag_context
        from frontend.media import build_place_cards
        text=resources.enter_context(TextRetriever(embedder=TextEmbedder(),sparse_embedder=SparseEmbedder()))
        image=resources.enter_context(ImageRetriever(embedder=ImageEmbedder()))
        search=resources.enter_context(SearchService(text_retriever=text,image_retriever=image))
        manifest['model_load_seconds']=perf_counter()-start
        write(out/'run.json',manifest)
        by_place={p['place_id']:p for p in meta['places']}
        by_photo={p['image_id']:p for p in meta['photos']}
        order=cases.copy();random.Random(16).shuffle(order)
        rows=[]
        with (out/'results.jsonl').open('w',encoding='utf-8') as stream:
            for index,case in enumerate(order,1):
                row={**case,'raw_ids':[],'final_ids':[]}
                start=perf_counter()
                try:
                    if case['kind']=='guide_section':
                        groups=text.search_grouped(case['query'],limit=20,page_ids=[str(p['page_id']) for p in pages if p['title']==case['city']])
                        row['raw_ids']=[hit.payload['chunk_id'] for group in groups for hit in group]
                    else:
                        scope=[p['place_id'] for p in meta['places'] if in_city(p,case['city'])]
                        groups=image.search_text_grouped(case['query'],limit=20,group_size=3,place_ids=scope)
                        row['raw_ids']=[group[0].place_id for group in groups if group]
                    row['raw_seconds']=perf_counter()-start
                    response=search.search(case['query'],route='text' if case['kind']=='guide_section' else 'hybrid',
                        location=case['city'],limit=20)
                    context=build_rag_context(response)
                    row['retrieval']=asdict(response)
                    row['context']=context
                    row['cards']=build_place_cards({'retrieval':row['retrieval'],'context':context})
                    if case['kind']=='guide_section':
                        row['final_ids']=unique([hit.payload['chunk_id'] for result in response.results for hit in [result,*result.evidence]])
                        row['context_section_hit']=any(e.get('section')==case['section'] for s in context['sources'] for e in s.get('excerpts',[]))
                    else:
                        row['final_ids']=[hit.place_id for hit in response.related_results]
                        row['wrong_city_results']=sum(not in_city(by_place.get(pid,{}),case['city']) for pid in row['final_ids'])
                        row['wrong_category_results']=sum(category(by_place.get(pid,{}))!=case['category'] for pid in row['final_ids']) if case['kind']=='category' else 0
                        row['wrong_identity_results']=sum(pid not in labels[case['case_id']] for pid in row['final_ids']) if case['kind']=='known_place' else 0
                        row['photo_binding_violations']=sum(by_photo.get(str(hit.point_id),{}).get('place_id')!=hit.place_id for hit in response.related_results)
                    row['card_wrong_city_count']=sum(not in_city(by_place.get(c['place_id'],{}),case['city']) for c in row['cards'])
                    row['card_wrong_category_count']=sum(category(by_place.get(c['place_id'],{}))!=case['category'] for c in row['cards']) if case['kind']=='category' else 0
                except Exception as error:
                    from src.utils.redaction import CredentialRedactor
                    row['error']={'type':type(error).__name__,'message':CredentialRedactor().text(str(error))[:1600]}
                row['seconds']=perf_counter()-start
                row['raw']=ranking(row['raw_ids'],labels[case['case_id']])
                row['final']=ranking(row['final_ids'],labels[case['case_id']])
                write(out/'cases'/(case['case_id']+'.json'),row)
                stream.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n');stream.flush()
                rows.append(row)
                print(f"{index}/100 {case['case_id']} {case['kind']} {'ERROR' if row.get('error') else 'ok'} raw={len(row['raw_ids'])} final={len(row['final_ids'])} {row['seconds']:.2f}s",flush=True)
        print('Verifying unchanged indexes and metadata...',flush=True)
        after,after_chunks=inventory(client,config,pages)
        write(out/'index_after.json',after)
        verification={'application_unchanged':application==sources(),'harness_unchanged':harness==hashes([*Path(__file__).parent.glob('*.py'),ROOT/'benchmarks/scripts/evaluate_retrieval.py']),
            'metadata_unchanged':digest(meta)==digest(metadata(store)),'index_unchanged':digest(before)==digest(after),
            'guide_chunks_unchanged':digest(sorted(chunks,key=lambda c:c['chunk_id']))==digest(sorted(after_chunks,key=lambda c:c['chunk_id'])),
            'unique_case_count':len({r['case_id'] for r in rows}),'labels_frozen_before_queries':True,'production_data_written':False}
        write(out/'verification.json',verification)
        if not all(verification[key] for key in ('application_unchanged','harness_unchanged','metadata_unchanged','index_unchanged','guide_chunks_unchanged')):
            raise RuntimeError('Inputs changed during retrieval evaluation; do not treat this as a consistent run.')
        manifest['completed_at']=datetime.now(timezone.utc).isoformat();write(out/'run.json',manifest)
        summary=summarize(out,cases,rows)
        write(out/'artifact_hashes.json',{p.name:sha256(p.read_bytes()).hexdigest() for p in out.glob('*') if p.is_file() and p.name!='artifact_hashes.json'})
        print(json.dumps(summary,indent=2),flush=True)


if __name__=='__main__':
    main()
