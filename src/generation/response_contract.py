"""Deterministic presentation checks; factual support still needs evidence review."""

import re
import unicodedata
from dataclasses import dataclass

from src.security.context import answer_safety_issues
from src.utils.language import query_language


def _fold(text):
    return ''.join(c for c in unicodedata.normalize('NFD', text.casefold())
                   if not unicodedata.combining(c)).replace('đ', 'd')


def _instructions(text):
    # Supplied text is not a request to change the answer's language or layout.
    return re.sub(r'```[\s\S]*?```|`[^`\n]*`|"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|(?<!\w)\'[^\'\n]+\'(?!\w)', ' ', text)


def _explicit_language(text):
    text = _fold(_instructions(text))
    if re.search(r'\b(?:bilingual|song ngu|both english and vietnamese|both vietnamese and english)\b', text):
        return 'other'
    matches = list(re.finditer(
        r'\b(?:answer|respond|reply|write|speak|use|translate|prefer|in|tra loi|viet|dich|su dung|bang)\b'
        r'[^.!?;\n]{0,65}?\b(english|vietnamese|tieng anh|tieng viet|french|japanese|chinese|korean|spanish|german)\b'
        r'|\b(english|vietnamese|tieng anh|tieng viet)\s+(?:please|only)\b', text))
    for match in reversed(matches):
        prefix = text[max(0, match.start()-18):match.start()]
        if re.search(r"\b(?:not|never|dont|don't|do not|khong|dung)\s*$", prefix):
            continue
        label = match[1] or match[2]
        return {'english': 'en', 'tieng anh': 'en', 'vietnamese': 'vi', 'tieng viet': 'vi'}.get(label, 'other')
    return None


@dataclass(frozen=True)
class ResponseContract:
    language: str | None
    layout: str | None = None
    literal_quotes: tuple[str, ...] = ()

    def payload(self):
        return {'language': self.language, 'layout': self.layout}


def resolve_response_contract(query, memory=None):
    explicit = _explicit_language(query)
    language = explicit
    if language is None:
        for record in reversed((memory or {}).get('long_term', [])):
            if record.get('kind') == 'preference' and record.get('key') == 'language':
                value = record.get('source_quote') or record.get('content', '')
                language = _explicit_language(value)
                if language is None:
                    language = {'english': 'en', 'vietnamese': 'vi', 'tieng anh': 'en',
                                'tieng viet': 'vi'}.get(_fold(record.get('content', '')).strip())
                if language:
                    break
    language = None if language == 'other' else language or query_language(_instructions(query))
    layout = None
    layout_request = _instructions(query).casefold()
    # Preserve distinctions lost by accent folding: bằng=using, bảng=table;
    # dùng=use, đừng=do not.
    for original, replacement in (('bằng', 'using'), ('dùng', 'use'), ('đừng', 'do not')):
        layout_request = re.sub(r'\b'+original+r'\b', replacement, layout_request)
    for clause in re.split(r'[.!?;\n]', _fold(layout_request)):
        # Only affirmative presentation requests, not explanations of formats.
        for match in re.finditer(r'\b(?:table|bang|bullet(?:ed)?(?: list)?|numbered list|danh sach danh so|danh sach co thu tu|gach dau dong)\b', clause):
            prefix, suffix = clause[:match.start()], clause[match.end():]
            if re.search(r"\b(?:no|not|without|avoid|dont|don't|do not|khong|dung|instead of|rather than)\b[^,;]{0,30}$", prefix):
                continue
            if (re.search(r'\b(?:as|in|use|give|show|return|provide|make|create|present|format|using|lap|tao|trinh bay|trong|so sanh|dang|liet ke)\b', prefix)
                    or re.match(r'\s*(?:only|please)\b', suffix)):
                layout = 'table' if match[0] in {'table', 'bang'} else (
                    'numbered_list' if match[0] in {'numbered list', 'danh sach danh so', 'danh sach co thu tu'} else 'bullets')
    quotes = tuple(match[0] for match in re.finditer(r'"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|(?<!\w)\'[^\'\n]+\'(?!\w)', query))
    return ResponseContract(language, layout, quotes)


def _table_present(answer):
    def cells(line):
        return re.split(r'(?<!\\)\|', line.strip().strip('|'))
    lines = answer.splitlines()
    for index in range(1, len(lines)-1):
        separator = cells(lines[index])
        if '|' not in lines[index] or not all(re.fullmatch(r'\s*:?-{3,}:?\s*', cell) for cell in separator):
            continue
        header, row = cells(lines[index-1]), cells(lines[index+1])
        if len(header) == len(row) == len(separator) and all(cell.strip() for cell in header) and any(cell.strip() for cell in row):
            return True
    return False


_EN = set('the is are was were this that these those it its has have will would can cannot could not no unknown from with for your you please source sources supplied recorded entry free closed open price hours at on and but according check confirm'.split())
_VI = set('toi minh ban chung nhung cac khong duoc co cua va la trong voi cho tu den theo nay du lieu nguon gia ve mo dong cua vao thu mien phi biet chua xac nhan thong tin tra loi tieng bao tang gio ngay'.split())


def _language_issues(answer, contract, context):
    if contract.language not in {'en', 'vi'}:
        return []
    prose = _instructions(re.sub(r'^\s*>.*$', '', answer, flags=re.M))
    # Proper names/addresses and faithful source quotations are not language drift.
    for source in context.get('sources', []):
        metadata = source.get('place_metadata') or {}
        for value in [source.get('name'), metadata.get('name'), metadata.get('address')]:
            if isinstance(value, str) and value:
                prose = prose.replace(value, ' ')
    prose = re.sub(r'\[[SW]\d+\]|https?://\S+', '', prose)
    for paragraph in re.split(r'\n\s*\n|(?<=[.!?])\s+', prose):
        words = re.findall(r'[a-z]+', _fold(paragraph))
        en, vi = sum(word in _EN for word in words), sum(word in _VI for word in words)
        if contract.language == 'en' and vi >= 3 and vi > en * 1.6:
            return ['Answer in English; preserve original proper names and quotations.']
        if contract.language == 'vi' and en >= 3 and en > vi * 1.6:
            return ['Answer in Vietnamese with full diacritics; preserve proper names and quotations.']
        if contract.language == 'vi' and vi >= 3 and vi > en and not re.search(r'[^\x00-\x7f]', paragraph):
            return ['Vietnamese prose must use standard spelling and full diacritics.']
    return []


def response_contract_issues(answer, context, contract, *, direct=False):
    """Return actionable violations without asking the grounding model for approval.

    No mandatory citation for unknowns: detecting uncited factual claims and
    checking the association of each fact with its citation remain semantic tasks.
    Language detection is deliberately conservative, not a universal classifier.
    """
    issues = answer_safety_issues(answer, context)
    if issues:
        return issues
    allowed = {source['source_id'] for source in context.get('sources', [])}
    if not direct and (context.get('weather') or {}).get('days'):
        allowed.add('W1')
    citation_text = answer
    for quote in contract.literal_quotes:
        citation_text = citation_text.replace(quote, ' ')
    citations = re.findall(r'\b[SW]\d+\b', citation_text, re.I)
    exact = re.findall(r'\[([SW]\d+)\]', citation_text)
    if direct and citations:
        issues.append('Direct answers cannot cite sources that were not retrieved.')
    elif set(exact) - allowed:
        issues.append('The answer cites source IDs that were not retrieved.')
    if len(citations) != len(exact) or re.search(r'\[\[[SW]\d+\]\]|\[[SW]\d+\]\(', citation_text):
        issues.append('Use exact [S1]/[W1] citations; put section names outside the brackets, with no bare source IDs.')
    stripped = answer.strip()
    if stripped.startswith(('```', '~~~')) or stripped.startswith(('{', '[')) and _looks_like_json(stripped):
        issues.append('Return readable Markdown without an outer code fence or JSON wrapper.')
    layout_text = re.sub(r'```[\s\S]*?```|~~~[\s\S]*?~~~', '', answer)
    if contract.layout == 'table' and not _table_present(layout_text):
        issues.append('Return the requested Markdown table with a header, separator and data rows.')
    if contract.layout == 'bullets' and not re.search(r'^\s*[-+*]\s+\S', layout_text, re.M):
        issues.append('Use the requested bullet list.')
    if contract.layout == 'numbered_list' and not re.search(r'^\s*\d+[.)]\s+\S', layout_text, re.M):
        issues.append('Use the requested numbered list.')
    return issues + _language_issues(answer, contract, context)


def _looks_like_json(text):
    import json
    try:
        return isinstance(json.loads(text), (dict, list))
    except ValueError:
        return False


def contract_limitation(contract):
    if contract.language == 'vi':
        return 'Mình chưa thể tạo câu trả lời đáp ứng yêu cầu về cách trình bày. Bạn vui lòng thử lại.'
    return 'I couldn’t produce an answer that meets the requested response format. Please try again.'
