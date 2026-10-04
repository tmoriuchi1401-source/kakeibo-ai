"""Source-scoped human routing answers. Never AI, grouping or posting authority."""
from copy import deepcopy
import re

from .drive_run_state import StateError, _aware
from .receipt_pdf_units import _digest

KINDS = {'一般': 'normal', '医療': 'medical', '給与': 'payroll', '判定不能': 'sensitive_unknown'}
FLAGS = {'authority_scope': ['page_kind_only'], 'gemini_allowed': False,
         'accounting_allowed': False, 'medical_handoff_allowed': False, 'archive_allowed': False}
FIELDS = {'source_file_id', 'source_content_hash', 'page_number', 'page_hash',
          'automatic_classification', 'human_classification', 'confirmed_at',
          'confirmation_digest', *FLAGS}


def kind_key(source_id, number):
    return _digest([source_id, number])


def answer(page, human, timestamp):
    value = {**FLAGS, 'source_file_id': page['source_file_id'],
             'source_content_hash': page['source_content_hash'], 'page_number': page['page_number'],
             'page_hash': page['page_hash'], 'automatic_classification': page['classification'],
             'human_classification': human, 'confirmed_at': timestamp}
    return {**value, 'confirmation_digest': _digest(value)}


def validate_kinds(records):
    if not isinstance(records, dict):
        raise StateError('page_kind_state_invalid')
    for key, record in records.items():
        try:
            if (set(record) != FIELDS or key != kind_key(record['source_file_id'], record['page_number']) or
                    not re.fullmatch(r'[A-Za-z0-9_-]{1,150}', record['source_file_id']) or
                    type(record['page_number']) is not int or not 1 <= record['page_number'] <= 50 or
                    any(not re.fullmatch(r'[0-9a-f]{64}', record[k]) for k in
                        ('source_content_hash', 'page_hash', 'confirmation_digest')) or
                    record['automatic_classification'] not in KINDS.values() or
                    record['human_classification'] not in KINDS.values() or
                    any(record.get(k) is not False for k in FLAGS if k!='authority_scope') or
                    record.get('authority_scope') != ['page_kind_only']):
                raise ValueError()
            _aware(record['confirmed_at'])
            page = {**record, 'classification': record['automatic_classification']}
            if record != answer(page, record['human_classification'], record['confirmed_at']):
                raise ValueError()
        except Exception:
            raise StateError('page_kind_state_invalid') from None


def current_answer(value, page):
    record = value.get('page_kinds', {}).get(kind_key(page['source_file_id'], page['page_number']))
    if record and all(record[k] == page[k] for k in
                      ('source_file_id', 'source_content_hash', 'page_number', 'page_hash')):
        if record['automatic_classification'] == page['classification']:
            return deepcopy(record)
    return None


class PageKindConfirmation:
    def __init__(self, grouping):
        self.grouping = grouping

    def current(self, source_id):
        """The injected source loader must verify current bytes AND page hashes."""
        g = self.grouping
        value = g.store.load()
        observations, record = g._current(value, source_id)
        from .receipt_pdf_grouping import _snapshot, _valid_proposal
        try: valid = record and record['proposal'] and _valid_proposal(record['proposal'], _snapshot(observations))
        except Exception: valid = False
        if not valid:
            raise StateError('page_kind_source_changed')
        proposal = record['proposal']
        answers = {p['page_number']: a for p in proposal['pages']
                   if (a := current_answer(value, p)) is not None}
        return value, proposal, answers

    def confirm(self, expected, selections, *, request_id='', intent_digest=''):
        value, proposal, previous = self.current(expected['source_file_id'])
        if any(expected.get(k) != proposal[k] for k in
               ('source_file_id', 'source_content_hash', 'proposal_digest', 'grouping_version')):
            raise StateError('stale_proposal')
        if not isinstance(selections, dict) or not selections:
            raise StateError('page_kind_request_invalid')
        updates = {}
        for number, selection in selections.items():
            if type(number) is not int or not 1 <= number <= proposal['page_count'] or selection not in KINDS:
                raise StateError('page_kind_request_invalid')
            page = proposal['pages'][number - 1]
            human = KINDS[selection]
            # Known sensitive evidence stays sticky even in routing answers.
            if page['classification'] in {'medical', 'payroll'} and human != page['classification']:
                raise StateError('page_kind_restriction_conflict')
            old = previous.get(number)
            if old and old['human_classification'] in {'medical','payroll'} and human != old['human_classification']:
                raise StateError('page_kind_restriction_conflict')
            updates[number] = old if old and old['human_classification'] == human else answer(page, human, self.grouping.clock())
        if all(previous.get(n) == a for n, a in updates.items()):
            return updates
        for n, a in updates.items():
            value.setdefault('page_kinds', {})[kind_key(proposal['source_file_id'], n)] = a
        after = value['records'][_digest(proposal['source_file_id'])]
        from .pdf_general_grouping import scope_current
        if after['confirmation'] and not scope_current(value, after['proposal']):
            after['confirmation'] = None
            after['status'] = 'grouping_required'
        # No OCR, names, amounts or manual payment inputs in this audit/store.
        self.grouping._event(value, 'page_kind', proposal['source_file_id'], proposal['source_content_hash'],
                             after, after, 'page_kind_confirmed', request_id=request_id, request_digest=intent_digest)
        value['audit'][-1]['confirmation_digest'] = _digest([a['confirmation_digest'] for a in updates.values()])
        self.grouping.store.save(value)  # mandatory v2 If-Match + exact read-back
        return updates
