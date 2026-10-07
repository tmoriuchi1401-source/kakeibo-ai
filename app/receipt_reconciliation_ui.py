"""Two-column cards/visibility requests in existing PDFページ確認 only.

Offline projection builder: no Sheet writes, new tab, trigger or authentication
side effects. Auth links are minted by the reviewed host, not by cell changes.
"""
from .receipt_audit import validate_identity, entity_key, digest, needs_attention
from .receipt_reconciliation_authority import DECISIONS
from .pdf_page_review import SCHEMA, SHEET_ID
from .drive_run_state import StateError

CHOICES = list(DECISIONS)


def reconciliation_card(identity, candidate_ledger_id, ledger_snapshot_digest):
    from .receipt_audit import identifier, checked_hash
    validate_identity(identity); identifier(candidate_ledger_id); checked_hash(ledger_snapshot_digest)
    ident = {**identity, 'stable_page_identity': identity['page_identity'],
             'schema': SCHEMA, 'kind': 'receipt_reconciliation', 'page_numbers': [identity['page_number']],
             'candidate_ledger_id': candidate_ledger_id, 'ledger_snapshot_digest': ledger_snapshot_digest}
    # No date, amount, facility or patient data copied from Medical history.
    rows = [('target','対象','ページ p'+str(identity['page_number'])),
            ('state','状態','既存記帳との対応確認待ち'),
            ('original','原本','今回の原本を見る'),
            ('notice','確認事項','原本と既存記帳を比較。同一なら再記帳せず紐付け、別ならこの組合せだけ重複候補を解消。'),
            ('reconciliation_decision','対応','判断できない'),
            ('reconciliation_auth_link','本人確認','選択後、本人認証して明示確定（セル変更だけでは確定しません）'),
            ('result','処理結果','未確定・会計writeなし')]
    readonly = [r for r in rows if r[0] not in {'reconciliation_decision','state','result'}]
    return {'identity': ident, 'token': digest([ident,readonly]), 'rows': rows}


def visible_cards(cards, current_states):
    """Current metadata only; no history scan and no cross-page global gate."""
    result = []
    for card in cards:
        ident = card['identity']
        if 'entity_key' in ident:
            key = ident['entity_key']
        elif ident.get('kind') == 'receipt_reconciliation':
            from .receipt_audit import IDENTITY_FIELDS
            key = entity_key({k:ident[k] for k in IDENTITY_FIELDS})
        else:
            result.append(card); continue
        state = current_states.get(key)
        if state is None or needs_attention(state): result.append(card)
    return result


def visibility_requests(existing_rows, card_states, *, sheet_id=SHEET_ID, start_row_index=0):
    """Hide terminal card rows without clearing cells or losing owner inputs.

    card_states is a trusted backend token -> current-state projection. Hidden
    cells are used only to find presentation rows, never to authorize decisions.
    """
    requests = []
    for offset, row in enumerate(existing_rows):
        if len(row) < 4 or row[2] != SCHEMA or row[3] not in card_states: continue
        state = card_states[row[3]]
        requests.append({'updateDimensionProperties': {'range': {'sheetId':sheet_id,
            'dimension':'ROWS','startIndex':start_row_index+offset,'endIndex':start_row_index+offset+1},
            'properties':{'hiddenByUser':not needs_attention(state)},'fields':'hiddenByUser'}})
    return requests
