"""Audit chunk counts against the tokenizer used for Wikivoyage embedding."""

from __future__ import annotations

import argparse
import csv
import heapq
import json
import os
from pathlib import Path

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.utils.utils import (
    batched, ensure_parent, get_sha256, load_config, read_jsonl,
    resolve_path, write_jsonl_record,
)


def audit_chunks(input_path: Path, output_directory: Path) -> dict:
    # This audit tokenizes only: it does not load model weights or embed data.
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    from transformers import AutoTokenizer

    config = load_config()
    model_name = config["text_embedding_model"]
    cache = resolve_path(config["cache_folder"])
    tokenizer = AutoTokenizer.from_pretrained(
        model_name, cache_dir=str(cache), local_files_only=config["models_local_files_only"], use_fast=True,
    )
    # Read the prompt from the same cached model configuration, rather than
    # assuming a prompt across different embedding models.
    from transformers.utils.hub import cached_file
    prompt_config_path = cached_file(
        model_name, "config_sentence_transformers.json",
        cache_dir=str(cache), local_files_only=config["models_local_files_only"],
    )
    prompt_config = json.loads(Path(prompt_config_path).read_text(encoding="utf-8"))
    prompts = prompt_config.get("prompts", {})
    prompt_name = next((name for name in ("document", "passage", "corpus") if name in prompts), None)
    if prompt_name is None:
        prompt_name = prompt_config.get("default_prompt_name")
    document_prompt = prompts.get(prompt_name, "")

    # Import the formatter so the audit stays aligned with the actual indexer.
    from src.data.indexing.wikivoyage_indexer import WikivoyageIndexer

    corrected = ensure_parent(output_directory / "chunks.token_verified.jsonl")
    report = ensure_parent(output_directory / "chunk_token_audit.csv")
    summary_path = output_directory / "chunk_token_audit.summary.json"
    if input_path.resolve() in (corrected.resolve(), report.resolve(), summary_path.resolve()):
        raise ValueError("Audit output must be separate from the input file.")
    source_hash = get_sha256(input_path)
    fields = [
        "chunk_id", "title", "section", "recorded_token_count", "actual_text_tokens",
        "actual_embedding_input_tokens", "difference", "text_characters",
        "has_coordinate_data", "exceeds_model_context", "dense_mask_gib_batch_32",
    ]
    summary = {
        "input": str(input_path), "input_sha256": source_hash,
        "tokenizer": model_name, "document_prompt": document_prompt,
        "model_context_tokens": tokenizer.model_max_length,
        "records": 0, "incorrect_recorded_counts": 0,
        "recorded_counts_equal_word_counts": 0, "coordinate_records": 0,
        "embedding_inputs_over_1024": 0, "embedding_inputs_over_2048": 0,
        "embedding_inputs_over_8192": 0, "embedding_inputs_over_model_context": 0,
        "max_text_tokens": 0, "max_embedding_input_tokens": 0,
        "token_count_definition": "Chunk text, no prompt, add_special_tokens=False",
        "embedding_input_definition": "Actual indexer text plus document prompt, add_special_tokens=True; no truncation",
    }
    largest = []
    seen_ids = set()
    with corrected.open("w", encoding="utf-8") as output, report.open("w", encoding="utf-8", newline="") as audit:
        writer = csv.DictWriter(audit, fieldnames=fields)
        writer.writeheader()
        for batch in batched(read_jsonl(input_path), 128):
            texts = [record["text"] for record in batch]
            inputs = [document_prompt + WikivoyageIndexer.build_embedding_text(record) for record in batch]
            body_ids = tokenizer(texts, add_special_tokens=False, truncation=False,
                                 return_attention_mask=False, return_token_type_ids=False)["input_ids"]
            input_ids = tokenizer(inputs, add_special_tokens=True, truncation=False,
                                  return_attention_mask=False, return_token_type_ids=False)["input_ids"]
            for record, body, submitted in zip(batch, body_ids, input_ids):
                chunk_id = record["chunk_id"]
                if chunk_id in seen_ids:
                    raise ValueError(f"Duplicate chunk ID: {chunk_id}")
                seen_ids.add(chunk_id)
                old_count = record.get("token_count")
                body_count, input_count = len(body), len(submitted)
                coordinates = '"coordinates"' in record["text"] or (
                    '],[' in record["text"] and record["text"].count('],[') >= 20
                )
                row = {
                    "chunk_id": chunk_id, "title": record["title"], "section": record["section"],
                    "recorded_token_count": old_count, "actual_text_tokens": body_count,
                    "actual_embedding_input_tokens": input_count,
                    "difference": body_count - old_count if isinstance(old_count, int) else "",
                    "text_characters": len(record["text"]), "has_coordinate_data": coordinates,
                    "exceeds_model_context": input_count > tokenizer.model_max_length,
                    "dense_mask_gib_batch_32": round(32 * min(input_count, tokenizer.model_max_length) ** 2 / 1024 ** 3, 6),
                }
                writer.writerow(row)
                write_jsonl_record(output, {**record, "token_count": body_count})
                summary["records"] += 1
                summary["incorrect_recorded_counts"] += old_count != body_count
                summary["recorded_counts_equal_word_counts"] += old_count == len(record["text"].split())
                summary["coordinate_records"] += coordinates
                for limit in (1024, 2048, 8192):
                    summary[f"embedding_inputs_over_{limit}"] += input_count > limit
                summary["embedding_inputs_over_model_context"] += input_count > tokenizer.model_max_length
                summary["max_text_tokens"] = max(summary["max_text_tokens"], body_count)
                summary["max_embedding_input_tokens"] = max(summary["max_embedding_input_tokens"], input_count)
                item = (input_count, chunk_id, json.dumps(row, ensure_ascii=False))
                if len(largest) < 20:
                    heapq.heappush(largest, item)
                elif item > largest[0]:
                    heapq.heapreplace(largest, item)
            if summary["records"] % 4096 == 0:
                print(f"Audited {summary['records']:,} records; {summary['incorrect_recorded_counts']:,} count mismatches", flush=True)
    if get_sha256(input_path) != source_hash:
        raise RuntimeError("The input changed during the audit. Run it again on the latest file.")
    summary["largest_inputs"] = [json.loads(row) for _, _, row in sorted(largest, reverse=True)]
    summary["corrected_output"] = str(corrected)
    summary["corrected_output_sha256"] = get_sha256(corrected)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "largest_inputs"}, indent=2), flush=True)
    return summary


if __name__ == "__main__":
    config = load_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=config["wikivoyage_chunks_path"])
    parser.add_argument("--output-directory", default=config["token_audit_directory"])
    arguments = parser.parse_args()
    audit_chunks(resolve_path(arguments.input), resolve_path(arguments.output_directory))
