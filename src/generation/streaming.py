"""Request-local answer previews; internal reasoning and tool JSON never stream."""

from contextlib import contextmanager
from contextvars import ContextVar
import re
from time import monotonic

from src.security.guardrails import MAX_ANSWER_CHARS, UnsafeInput
from src.utils.redaction import CredentialRedactor


_CURRENT = ContextVar('vistra_answer_stream', default=None)


class AnswerStream:
    def __init__(self, callback, *, clock=monotonic):
        self.callback, self.clock = callback, clock
        self.redactor = CredentialRedactor()
        self.raw, self.last_text, self.last_sent = '', '', 0

    def begin(self):
        self.raw, self.last_text, self.last_sent = '', '', 0
        self.callback({'text': '', 'phase': 'writing'})

    def feed(self, delta):
        if not isinstance(delta, str):
            raise ValueError('Answer deltas must be text.')
        self.raw += delta
        if len(self.raw) > MAX_ANSWER_CHARS:
            raise UnsafeInput('answer_size', 'The streamed answer exceeds its size limit.')
        # Retain incomplete words so credentials split across provider events
        # cannot leak a prefix before the redactor has the complete value.
        boundaries = list(re.finditer(r'\s+', self.raw))
        end = boundaries[-1].end() if boundaries else 0
        for secret in self.redactor._secrets:
            start = self.raw.rfind(secret)
            if start >= 0 and start < end < start+len(secret):
                end = start
            for size in range(min(len(secret)-1, len(self.raw)), 0, -1):
                if self.raw.endswith(secret[:size]):
                    end = min(end, len(self.raw)-size)
                    break
        safe = self.redactor.text(self.raw[:end])
        now = self.clock()
        if safe != self.last_text and (not self.last_text or now-self.last_sent >= .05):
            self.last_text, self.last_sent = safe, now
            # Snapshots tolerate dropped queue events and reconnect-safe final
            # reconciliation, with at most 20 normal updates per second.
            self.callback({'text': safe, 'phase': 'writing'})

    def finish(self):
        self.last_text = self.redactor.text(self.raw)
        self.callback({'text': self.last_text, 'phase': 'checking'})


def current_answer_stream():
    return _CURRENT.get()


@contextmanager
def answer_stream(callback):
    stream = AnswerStream(callback)
    token = _CURRENT.set(stream)
    try:
        yield stream
    finally:
        _CURRENT.reset(token)
