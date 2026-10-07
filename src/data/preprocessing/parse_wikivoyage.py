from pathlib import Path
import xml.etree.ElementTree as ET

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.utils.utils import ensure_parent, load_config, resolve_path, write_jsonl_record


def parse_pages(input_path=None, output_path=None):
    from mwsql import Dump
    config = load_config()
    input_path = resolve_path(input_path, config["wikivoyage_pages_dump"])
    output_path = resolve_path(output_path, config["wikivoyage_pages_path"])
    if input_path == output_path:
        raise ValueError("Page output must be separate from the source dump.")
    output_path = ensure_parent(output_path)

    dump = Dump.from_file(input_path)

    print("Columns:")
    print(dump.col_names)

    columns = {name: i for i, name in enumerate(dump.col_names)}
    count = 0

    with output_path.open("w", encoding="utf-8") as out:
        for row in dump.rows(convert_dtypes=True):

            name_space = row[columns["page_namespace"]]
            is_redirect = row[columns["page_is_redirect"]]

            if name_space != 0 or is_redirect:
                continue

            page_id = row[columns["page_id"]]
            title = row[columns["page_title"]].replace("_", " ")

            record = {
                "page_id": page_id,
                "title": title
            }

            write_jsonl_record(out, record)

            count += 1

    print(f"Saved {count:,} pages")
    print(f"Output: {output_path}")



def parse_geo(input_path=None, output_path=None):
    from mwsql import Dump
    config = load_config()
    input_path = resolve_path(input_path, config["wikivoyage_geo_dump"])
    output_path = resolve_path(output_path, config["wikivoyage_geo_tags_path"])
    if input_path == output_path:
        raise ValueError("Geo output must be separate from the source dump.")
    output_path = ensure_parent(output_path)

    dump = Dump.from_file(input_path)

    print("Columns:")
    print(dump.col_names)

    columns = {name: i for i, name in enumerate(dump.col_names)}
    count = 0

    with output_path.open("w", encoding="utf-8") as out:
        for row in dump.rows(convert_dtypes=True):

            page_id = row[columns["gt_page_id"]]
            latitude = row[columns["gt_lat"]]
            longitude = row[columns["gt_lon"]]
            primary = row[columns["gt_primary"]]

            if not primary:
                continue

            record = {
                "page_id": page_id,
                "latitude": latitude,
                "longitude": longitude
            }

            write_jsonl_record(out, record)

            count += 1

    print(f"Saved {count:,} pages")
    print(f"Output: {output_path}")


def clean_wikitext(text: str) -> str:
    import mwparserfromhell
    if not text:
        return ""

    code = mwparserfromhell.parse(text)

    return code.strip_code(normalize=True, collapse=True).strip()


def parse_articles(input_path=None, output_path=None):
    config = load_config()
    input_path = resolve_path(input_path, config["wikivoyage_articles_dump"])
    output_path = resolve_path(output_path, config["wikivoyage_articles_path"])
    if input_path == output_path:
        raise ValueError("Article output must be separate from the source dump.")
    output_path = ensure_parent(output_path)
    count = 0

    context = ET.iterparse(input_path, events=("start", "end"))
    _, root = next(context)

    with output_path.open("w", encoding="utf-8") as out:
        for event, elem in context:

            if event != "end" or elem.tag.rsplit("}", 1)[-1] != "page":
                continue

            title_elem = elem.find("./{*}title")
            id_elem = elem.find("./{*}id")
            ns_elem = elem.find("./{*}ns")
            text_elem = elem.find("./{*}revision/{*}text")

            if (title_elem is None or id_elem is None or ns_elem is None):
                elem.clear()
                root.clear()
                continue

            namespace = int(ns_elem.text)

            if namespace != 0 or elem.find("./{*}redirect") is not None:
                elem.clear()
                root.clear()
                continue

            page_id = int(id_elem.text)

            title = title_elem.text or ""

            raw_text = (text_elem.text if text_elem is not None else "")

            clean_text = clean_wikitext(raw_text)

            if not clean_text:
                elem.clear()
                root.clear()
                continue

            record = {
                "page_id": page_id,
                "title": title,
                "text": clean_text
            }

            write_jsonl_record(out, record)

            count += 1

            if count % 1000 == 0:
                print(f"Processed {count:,} articles")

            elem.clear()
            root.clear()

    print(f"Saved {count:,} articles")
    print(f"Output: {output_path}")




if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Parse a configured Wikivoyage dump.")
    parser.add_argument("kind", choices=("pages", "geo", "articles"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    {"pages": parse_pages, "geo": parse_geo, "articles": parse_articles}[args.kind](args.input, args.output)
