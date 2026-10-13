"""Build tokenizer-sized Wikivoyage chunks for dense and sparse indexing."""

import re
from functools import lru_cache
from pathlib import Path

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.utils.utils import (
    clean_text,
    ensure_parent,
    load_config,
    read_jsonl,
    resolve_path,
    slugify,
    write_jsonl_record,
)
from src.data.preprocessing.wikivoyage_geometry import remove_geojson_text

@lru_cache(maxsize=1)
def get_tokenizer():
    from transformers import AutoTokenizer

    config = load_config()
    return AutoTokenizer.from_pretrained(
        config["text_embedding_model"],
        cache_dir=str(resolve_path(config["cache_folder"])),
        local_files_only=config["models_local_files_only"],
    )


# The processed Wikivoyage dump has had most MediaWiki formatting removed.
# Section titles therefore appear both as ``== Eat ==`` and as plain lines.
# Match known section titles only so ordinary short paragraphs are not treated
# as headings. Subsections such as "By train" remain inside their parent
# section (for example, "Get in").
section_names = {
    "Understand",
    "Get in",
    "Get around",
    "See",
    "Do",
    "Buy",
    "Eat",
    "Drink",
    "Sleep",
    "Connect",
    "Go next",
    "Stay safe",
    "Stay healthy",
    "Cope",
    "Respect",
    "Learn",
    "Work",
    "Talk",
    "See also",
    "Other destinations",
    "Cities",
    "Regions",
    "Towns",
    "Nearby",
    "History",
    "Geography",
    "Climate",
    "Landscape",
    "Flora and fauna",
    "Fees and permits",
    "Visitor information",
    "Transportation",
    "Events",
    "Districts",
    "Orientation",
    "Tourist information",
    "Get out",
}

section_pattern = re.compile(
    r"^[ \t]*(?:={2,6}[ \t]*)?"
    r"(" + "|".join(
        re.escape(name) for name in sorted(section_names, key=len, reverse=True)
    ) + r")"
    r"(?:[ \t]*={2,6})?[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)



def parse_sections(text: str) -> list[dict]:

    text = clean_text(text)

    if not text:
        return []

    matches = list(section_pattern.finditer(text))

    if not matches:
        return [
            {
                "section": "General",
                "text": text
            }
        ]


    sections = []

    intro = text[: matches[0].start()].strip()

    if intro:
        sections.append(
            {
                "section": "Introduction",
                "text": intro
            }
        )

    for i, match in enumerate(matches):
        section_name = clean_text(match.group(1))
        # Use canonical capitalization even if a source heading is lowercase.
        section_name = next(
            name for name in section_names if name.casefold() == section_name.casefold()
        )

        start = match.end()

        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            end = len(text)

        section_text = clean_text(text[start:end])

        if section_text:
            sections.append(
                {
                    "section": section_name,
                    "text": section_text,
                }
            )

    return sections


def count_tokens(text: str) -> int:
    return len(get_tokenizer().encode(text, add_special_tokens=False))


def truncate_tokens(
    text: str,
    max_tokens: int,
) -> str:

    tokenizer = get_tokenizer()
    tokens = tokenizer.encode(text, add_special_tokens=False)
    tokens = tokens[:max_tokens]

    return tokenizer.decode(tokens)


def chunk_section(
    text: str,
    chunk_size: int | None = None,
    overlap: int | None = None,
    min_tokens: int | None = None,
) -> list[str]:
    if chunk_size is None or overlap is None or min_tokens is None:
        config = load_config()
        chunk_size = chunk_size if chunk_size is not None else config["chunk_size"]
        overlap = overlap if overlap is not None else config["chunk_overlap"]
        min_tokens = min_tokens if min_tokens is not None else config["chunk_min_tokens"]
    if any(type(value) is not int for value in (chunk_size, overlap, min_tokens)) or not 0 <= overlap < chunk_size or not 1 <= min_tokens <= chunk_size:
        raise ValueError("Require 0 <= overlap < chunk_size and 1 <= min_tokens <= chunk_size.")
    tokenizer = get_tokenizer()
    encoded = tokenizer.encode(clean_text(text), add_special_tokens=False)
    chunks = []
    for start in range(0, len(encoded), chunk_size - overlap):
        piece = encoded[start:start + chunk_size]
        if len(piece) >= min_tokens:
            chunks.append(tokenizer.decode(piece, skip_special_tokens=True).strip())
        if start + chunk_size >= len(encoded):
            break
    return chunks


def build_embedding_text(
    title: str,
    section: str,
    text: str,
) -> str:

    return (
        f"Destination: {title}\n"
        f"Section: {section}\n\n"
        f"{text}"
    )


def process_articles(
    input_path: Path | None = None,
    output_path: Path | None = None,
):

    config = load_config()
    input_path = resolve_path(input_path, config["wikivoyage_articles_path"])
    output_path = resolve_path(output_path, config["wikivoyage_chunks_path"])
    if input_path == output_path:
        raise ValueError("Chunk output must be separate from the source articles.")
    output_path = ensure_parent(output_path)

    article_count = 0
    chunk_count = 0

    with output_path.open("w", encoding="utf-8") as output_file:
        for article in read_jsonl(input_path):

            article_count += 1

            page_id = article.get("page_id", article.get("id"))
            if page_id is None:
                raise ValueError("Article is missing its Wikivoyage page_id.")

            title = clean_text(article.get("title", ""))

            text = remove_geojson_text(
                article.get("text", article.get("content", ""))
            )

            if not title or not text:
                continue

            sections = parse_sections(text)
            section_chunk_counts = {}

            for section in sections:

                section_name = section["section"]
                section_text = section["text"]

                chunks = chunk_section(section_text, config["chunk_size"], config["chunk_overlap"], config["chunk_min_tokens"])

                chunk_index_offset = section_chunk_counts.get(section_name, 0)

                for index, chunk_text in enumerate(chunks):
                    chunk_index = chunk_index_offset + index

                    chunk_id = (
                        f"{page_id}_"
                        f"{slugify(section_name)}_"
                        f"{chunk_index}"
                    )

                    record = {
                        "chunk_id": chunk_id,
                        "page_id": page_id,
                        "title": title,
                        "section": section_name,
                        "chunk_index": chunk_index,

                        "text": chunk_text,

                        "embedding_text":
                            build_embedding_text(
                                title,
                                section_name,
                                chunk_text,
                            ),

                        "token_count":
                            count_tokens(chunk_text),

                        "source":
                            article.get(
                                "source",
                                "wikivoyage",
                            ),

                        "url":
                            article.get("url"),

                        "lat":
                            article.get(
                                "lat",
                                article.get("latitude"),
                            ),

                        "lon":
                            article.get(
                                "lon",
                                article.get("longitude"),
                            ),
                    }

                    write_jsonl_record(output_file, record)

                    chunk_count += 1

                section_chunk_counts[section_name] = (
                    chunk_index_offset + len(chunks)
                )

    print()
    print("Finished.")
    print(f"Articles processed : {article_count:,}")
    print(f"Chunks created     : {chunk_count:,}")
    print(f"Output             : {output_path}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Override the configured article JSONL file.")
    parser.add_argument("--output", type=Path, help="Override the configured chunk JSONL file.")
    args = parser.parse_args()
    process_articles(args.input, args.output)


if __name__ == "__main__":
    main()
