"""Transient comparisons of existing receipt IO only; no IO or authority.

Values, digests, ordinals and monotonic times never cross the log boundary.
The transport's existing narrow metadata projection has no version: report
unavailable rather than infer one from an ETag or expand a GET projection.
"""
from hashlib import sha256
import logging
from time import monotonic

LOG = logging.getLogger(__name__)
PHASES = ('pre', 'cas', 'post')
POINTS = ('outer_before', 'transport_before', 'transport_after', 'outer_after')
PAIRS = tuple(phase + '_' + pair for phase in PHASES for pair in (
    'outer_transport', 'transport_pair', 'outer_pair', 'transport_outer')) + (
    'cas_precheck', 'cas_boundary')
RELATIONS = {
    'version': ('same', 'up', 'down', 'representation_changed', 'unavailable'),
    'etag': ('same', 'different', 'unavailable'),
    'order': ('forward', 'unexpected'),
    'elapsed': ('under_100ms', 'under_1s', 'under_5s', 'at_least_5s', 'unavailable'),
}
# Constructed solely from closed source literals; never from Drive data.
POINT_CODES = {(phase, point): 'receipt_get_diag_' + phase + '_' + point
               for phase in PHASES for point in POINTS}
PAIR_CODES = {(pair, field, relation): 'receipt_get_diag_' + pair + '_' + field + '_' + relation
              for pair in PAIRS for field, relations in RELATIONS.items() for relation in relations}
STATUS_CODES = frozenset((
    'receipt_get_diag_begin', 'receipt_get_diag_cas_enter',
    'receipt_get_diag_cas_acknowledged', 'receipt_get_diag_cas_unacknowledged',
    'receipt_get_diag_post_body_same', 'receipt_get_diag_post_body_different',
    'receipt_get_diag_post_body_unavailable', 'receipt_get_diag_post_incomplete',
    'receipt_get_diag_finished',
))
CODES = frozenset(POINT_CODES.values()) | frozenset(PAIR_CODES.values()) | STATUS_CODES


def _emit(code):
    if code in CODES:
        LOG.warning(code)  # No formatting args, exception, extra, raw value or digest.


def _number(value):
    if type(value) is str and 0 < len(value) <= 20 and value.isascii() and value.isdecimal():
        return int(value)
    return None


class ExistingGetDiagnostics:
    """One conditional-write observation, cleared on every exit.

    It cannot make a request, authorize a transition, reset an adapter stop,
    retry a write, or alter a returned metadata dict. Failures are discarded
    by observe(), so telemetry cannot change the guarded operation's result.
    """
    def __init__(self):
        self.phase = None
        self.samples = {}
        self.planned = None
        self.ordinal = 0
        self.body_seen = False

    def begin(self, proposed):
        self.phase = 'pre'
        self.samples = {}
        self.ordinal = 0
        self.body_seen = False
        self.planned = sha256(proposed).digest()
        _emit('receipt_get_diag_begin')

    def enter_cas(self):
        self.phase = 'cas'
        _emit('receipt_get_diag_cas_enter')

    def cas_ok(self):
        self.phase = 'post'
        _emit('receipt_get_diag_cas_acknowledged')

    def metadata(self, point, value):
        if self.phase not in PHASES or point not in POINTS:
            return
        version = value.get('version') if type(value) is dict else None
        etag = value.get('etag') if type(value) is dict else None
        self.ordinal += 1
        self.samples[self.phase, point] = (
            version if type(version) is str else None,
            etag if type(etag) is str else None, self.ordinal, monotonic())
        _emit(POINT_CODES[self.phase, point])
        if point == 'transport_before':
            self.compare((self.phase, 'outer_before'), (self.phase, point), self.phase + '_outer_transport')
            if self.phase == 'cas':
                self.compare(('pre', 'outer_after'), ('cas', point), 'cas_precheck')
        elif point == 'transport_after':
            self.compare((self.phase, 'transport_before'), (self.phase, point), self.phase + '_transport_pair')
        elif point == 'outer_after':
            self.compare((self.phase, 'outer_before'), (self.phase, point), self.phase + '_outer_pair')
            self.compare((self.phase, 'transport_after'), (self.phase, point), self.phase + '_transport_outer')
        elif self.phase == 'post':
            self.compare(('pre', 'outer_after'), ('post', point), 'cas_boundary')

    def compare(self, first, second, pair):
        if first not in self.samples or second not in self.samples:
            return  # Short-circuited GETs remain unobserved, never fetched.
        left, right = self.samples[first], self.samples[second]
        old, new = _number(left[0]), _number(right[0])
        version = ('unavailable' if old is None or new is None else
                   'same' if left[0] == right[0] else 'up' if new > old else
                   'down' if new < old else 'representation_changed')
        etag = ('unavailable' if left[1] is None or right[1] is None else
                'same' if left[1] == right[1] else 'different')
        elapsed = right[3] - left[3]
        timing = ('unavailable' if elapsed < 0 else 'under_100ms' if elapsed < .1 else
                  'under_1s' if elapsed < 1 else 'under_5s' if elapsed < 5 else 'at_least_5s')
        for field, relation in (
            ('version', version), ('etag', etag),
            ('order', 'forward' if right[2] > left[2] else 'unexpected'), ('elapsed', timing),
        ):
            _emit(PAIR_CODES[pair, field, relation])

    def body(self, raw):
        if self.phase == 'post':
            self.body_seen = True
            relation = ('unavailable' if type(raw) is not bytes or self.planned is None else
                        'same' if sha256(raw).digest() == self.planned else 'different')
            _emit('receipt_get_diag_post_body_' + relation)

    def finish(self):
        phase, complete = self.phase, self.body_seen and ('post', 'outer_after') in self.samples
        # Clear before logging so even a broken logging handler retains no values.
        self.phase, self.samples, self.planned = None, {}, None
        self.ordinal, self.body_seen = 0, False
        if phase == 'cas':
            _emit('receipt_get_diag_cas_unacknowledged')
        elif phase == 'post' and not complete:
            _emit('receipt_get_diag_post_incomplete')
        if phase is not None:
            _emit('receipt_get_diag_finished')


def observe(io, event, *values):
    """Diagnostic faults cannot change any existing request or stop condition."""
    try:
        observer = getattr(io, '_receipt_get_diagnostics', None)
        if observer is not None and event in ('begin', 'enter_cas', 'cas_ok', 'metadata', 'body', 'finish'):
            getattr(observer, event)(*values)
    except Exception:
        pass  # No exception text, traceback, fallback IO or retry.
