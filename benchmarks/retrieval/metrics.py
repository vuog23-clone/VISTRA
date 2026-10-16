"""Finite ranking metrics. Failed requests never disappear from denominators."""

from math import log2


def unique(values):
    return list(dict.fromkeys(values))


def ranking(ids, relevant, *, k=20):
    ids, relevant = unique(ids)[:k],set(relevant)
    hit_ranks = [i for i,value in enumerate(ids,1) if value in relevant]
    gains = [1 if value in relevant else 0 for value in ids[:10]]
    ideal = sum(1/log2(i+2) for i in range(min(10,len(relevant))))
    return {'returned':len(ids),'relevant_pool':len(relevant),
        'precision_at_20':len(hit_ranks)/k,
        'precision_of_returned_at_20':len(hit_ranks)/len(ids) if ids else 0.0,
        'recall_at_20':len(hit_ranks)/len(relevant) if relevant else None,
        'recall_at_20_ceiling':min(k,len(relevant))/len(relevant) if relevant else None,
        'ndcg_at_10':sum(g/log2(i+2) for i,g in enumerate(gains))/ideal if ideal else None,
        'hit_at_5':any(i<=5 for i in hit_ranks) if relevant else None,
        'reciprocal_rank_at_20':1/hit_ranks[0] if hit_ranks else 0.0 if relevant else None,
        'correct_empty':not ids if not relevant else None}
