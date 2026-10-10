"""Offline projection proposal: one link in the existing PDF review card.

Trusted runner prepares a request; tapping/selection is never authority.
No Sheet client, onEdit hook, Apps Script, writer or live dispatch here.
"""
from .page_receipt_review import general_review_card


def authenticated_general_card(page, gateway):
    card = general_review_card(page)
    rid = gateway.prepare(page)
    card['rows'].append(('authenticated_general_confirm', '本人確認',
                         '一般レシートとして確定'))
    card['confirmation_link'] = {
        'field': 'authenticated_general_confirm',
        'url': gateway.review_link(gateway.origin + '/confirm', rid),
        'request_id': rid,
    }
    return card
