"""Offline HTML preview of Stage 1. Reads snapshots; no live service calls."""
import argparse
from dataclasses import asdict
from datetime import date, timedelta
from html import escape
import json
from pathlib import Path
import re

from .unified_receipt_review import cell_rows, project, inventory, annotate_posting_receipts

KINDS = {'normal':'一般', 'medical':'医療', 'intake':'種類・受付確認',
         'receipt_reconciliation':'既存記帳照合', 'grouping':'PDF構造',
         'page_kind':'ページ種類', 'page_privacy':'ページprivacy確認',
         'general_receipt_item_review':'一般明細'}
ACCEPTED = {'applied':'反映処理', 'closed_user':'本人の判断', 'closed_machine':'自動判定',
            'grouping_confirmed':'PDF構造確認', 'pending':'入力処理', 'imported':'記帳処理'}

CSS = '''
*{box-sizing:border-box}body{margin:0;background:#f4f6fa;color:#15273b;font:15px/1.6 system-ui,sans-serif}
main{max-width:1400px;margin:auto;padding:24px}h1{font-size:26px;margin:0}h2{font-size:19px}
.banner{padding:12px 16px;background:#fff4d7;border-left:5px solid #bd7000;margin:16px 0}
.summary{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:1400px;background:white;table-layout:fixed}
th:nth-child(1){width:145px}th:nth-child(2){width:180px}th:nth-child(3){width:130px}th:nth-child(4){width:85px}
th:nth-child(5){width:90px}th:nth-child(6){width:105px}th:nth-child(7){width:150px}th:nth-child(8){width:80px}
th:nth-child(9){width:105px}th:nth-child(10){width:210px}th:nth-child(11){width:100px}
th,td{padding:10px;border-bottom:1px solid #d7e0ea;text-align:left;vertical-align:top}
th{background:#e3eaf3;font-size:13px}td{overflow-wrap:anywhere}a{color:#0759b2;overflow-wrap:anywhere}a:focus{outline:3px solid #d78b05}
.case{border:2px solid #536d88;background:white;border-radius:8px;margin:20px 0;overflow:hidden;scroll-margin-top:12px}
.band{background:#193d64;color:white;padding:12px 16px;overflow-wrap:anywhere;font-weight:700}
.band.held{background:#8c4d0c}.body{padding:12px 16px}.hierarchy{color:#526579;overflow-wrap:anywhere}
.field{display:grid;grid-template-columns:180px 1fr;border-bottom:1px solid #e2e8ef;padding:10px 0;gap:12px}
.field b{color:#344c65}.value{white-space:pre-wrap;overflow-wrap:anywhere;min-width:0}
.end{padding:8px 16px;background:#e3eaf3;font-size:13px;overflow-wrap:anywhere}.repeat{background:#e3eaf3;color:#193d64}
.tag{font-size:12px;color:#5b6877}.issue{color:#8a4204}details{margin:20px 0}summary{cursor:pointer;padding:12px}
@media(max-width:600px){main{padding:12px}h1{font-size:22px}.summary table{min-width:0}.summary table,.summary tbody,.summary tr,.summary td{display:block;width:100%}
.summary thead{display:none}.summary tr{border:1px solid #c3d0df;border-radius:6px;margin-bottom:12px;padding:8px}
.summary td{border:0;padding:3px 6px;overflow-wrap:anywhere}.summary td:before{content:attr(data-label)'：';font-weight:700}
.field{grid-template-columns:1fr;gap:3px}.body{padding:10px 12px}.band{padding:12px}.hierarchy{font-size:13px}}
'''


def safe_link(uri, label):
    if re.fullmatch(r'https://drive\.google\.com/file/d/[A-Za-z0-9_-]{10,150}/view(?:#page=\d+)?', str(uri)):
        return '<a rel="noreferrer" href="'+escape(uri, quote=True)+'">'+escape(label)+'</a>'
    return escape(label)


def pages_label(case):
    return '・'.join('p'+str(n) for n in case.pages) or '原本単位（ページ対応未確定）'


def heading(case):
    state, _ = case.display()
    return f'{case.case_id}・{pages_label(case)}・レシート{case.identity.get("receipt_index", "未確定")}・{state}'


def displayed_date(value):
    if type(value) in (int, float) and value == int(value):
        try:
            return (date(1899,12,30)+timedelta(days=value)).isoformat()
        except (OverflowError, ValueError):
            pass
    return value


def render(cases):
    def e(value):
        return escape(str(value))
    def detail(case):
        state, next_step = case.display()
        parts = [f'<article class="case" id="{case.case_id}"><div class="band {"held" if case.issues else ""}">{e(heading(case))}</div><div class="body">',
                 f'<div class="hierarchy">原本 {e(case.source_id)} → {e(pages_label(case))} → レシート {e(case.identity.get("receipt_index", "未確定"))}</div>',
                 f'<p><b>次にすること：</b>{e(next_step)}</p>',
                 '<p class="tag">読み取り専用。入力・本人確認・記帳操作の接続は停止中です。</p>']
        if case.accepted or state == '処理待ち':
            parts.append(f'<div class="banner">受付内容：{e(case.accepted or ACCEPTED.get(case.authority_status,"受付証跡を未照合"))}<br>最終処理時刻：{e(case.last_processed_at or "未照合")}<br>未完了理由：{e(case.unfinished_reason or next_step)}</div>')
        parts += [f'<p class="issue">{e(issue)}</p>' for issue in case.issues]
        visible = [r for r in case.fields if not (case.origin == '領収書確認' and case.kind != 'medical'
                   and r[0] in {'date','merchant','amount','category','payment'} and r[2] == '')]
        for n, (marker, label, value) in enumerate(visible):
            if n and n % 8 == 0:
                parts.append('<div class="band repeat">'+e(heading(case))+'</div>')
            original = value
            if marker == 'original' and value == '原本を開く' and re.fullmatch(r'[A-Za-z0-9_-]{10,150}',case.source_id):
                original = 'https://drive.google.com/file/d/'+case.source_id+'/view'+('#page='+str(case.pages[0]) if case.pages else '')
            text = safe_link(original, '原本を開く') if marker == 'original' else e(displayed_date(value) if marker == 'date' else value)
            parts.append(f'<div class="field" data-field="{e(marker)}"><b>{e(label)}</b><div class="value">{text}</div></div>')
        parts.append(f'</div><div class="end">案件終了：{e(case.case_id)}　<a href="#list">一覧へ戻る</a></div></article>')
        return ''.join(parts)
    active = [c for c in cases if not c.history and not c.synthetic and c.display()[0] != '完了']
    history = [c for c in cases if c not in active]
    labels = ['案件番号','原本','対象ページ','レシート番号','種別','日付','店舗・施設','金額','現在の状態','次にすること','詳細']
    out = ['<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
           '<title>領収書確認・統合プレビュー</title><style>'+CSS+'</style><main><h1>領収書確認</h1>',
           '<div class="banner">段階1・読み取り専用プレビュー<br>未完了案件の確認窓口です。identity対応未確定のため、本番操作はまだ接続していません。</div>',
           f'<h2 id="list">未完了案件 {len(active)}件</h2><div class="summary"><table><thead><tr>',
           ''.join('<th>'+x+'</th>' for x in labels), '</tr></thead><tbody>']
    for case in active:
        state, next_step = case.display()
        vals = [case.case_id, case.source_id, pages_label(case), case.identity.get('receipt_index','未確定'),
                KINDS.get(case.kind,case.kind), displayed_date(case.value('date') or case.summary.get('date','未確認')),
                case.value('merchant') or case.value('facility') or case.summary.get('merchant','未確認'),
                case.value('amount') or case.summary.get('amount','未確認'), state, next_step]
        out.append('<tr>'+''.join(f'<td data-label="{label}">{e(v)}</td>' for label,v in zip(labels,vals))+
                   f'<td data-label="詳細"><a href="#{case.case_id}">詳細を開く</a></td></tr>')
    out.append('</tbody></table></div><h2>案件詳細</h2>')
    out.extend(detail(c) for c in active)
    if history:
        out.append(f'<details><summary>旧版・完了証跡・テストの参照 {len(history)}件（入力保持）</summary>')
        out.extend(detail(c) for c in history)
        out.append('</details>')
    out.append('</main></html>')
    return ''.join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt-snapshot', type=Path, required=True)
    parser.add_argument('--pdf-snapshot', type=Path, required=True)
    parser.add_argument('--confirmation', type=Path, required=True)
    parser.add_argument('--posting-receipt', type=Path, nargs=2, action='append', default=[], metavar=('CANDIDATE','NATIVE_RECEIPT'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    read = lambda p: json.loads(p.read_bytes())
    pdf_rows = cell_rows(read(args.pdf_snapshot),'PDFページ確認')
    cases = project(cell_rows(read(args.receipt_snapshot),'領収書確認'),
                    pdf_rows, read(args.confirmation)['confirmation_items'])
    annotate_posting_receipts(cases,pdf_rows,[(read(c),read(r)) for c,r in args.posting_receipt])
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/'preview.html').write_text(render(cases), encoding='utf8')
    (args.output/'cases.json').write_text(json.dumps([asdict(c) for c in cases], ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(inventory(cases), ensure_ascii=True))


if __name__ == '__main__':
    main()
