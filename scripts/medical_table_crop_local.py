"""Bounded ruled-table crops and offline inspection; never an outbound authority.

Detection uses OpenCV already provisioned in the private OCR runtime. OCR runs
once on each original image, not again on the crops. Manual images are opened
only after automatic results are frozen. No OCR strings or amounts are logged.
"""
from __future__ import annotations

import argparse
import hashlib
import html
from io import BytesIO
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import unicodedata

from PIL import Image, ImageDraw
from scripts.crop_medical_vision_local import source_cards
from scripts.evaluate_medical_ai_structured_offline import _images, _runtime
from app.medical_ocr_observation_shadow import ReceiptImage
from app.medical_rapidocr_shadow import RapidOcrShadowAdapter
from app.medical_rapidocr_worker import offline_guard

VERSION = 'medical-ruled-table-local-v1'
# Inspection/ranking hints only, not parser labels or production thresholds.
PAYMENT = ('支払', '領収', '請求', '負担金', '預り', '預かり', '釣銭', 'お釣')
PRIVATE = ('氏名', '患者', '番号', '保険', '生年月日', '住所', '電話', 'TEL',
           '診療', '病名', '処方', '薬剤', '検査', '手術', '注射', '投薬', 'リハビリ')
WARNINGS = {
    'private_or_clinical_suspected': '個人情報・保険・診療情報に関係する語を検出（過剰警告を含む）',
    'unrecognized_or_low_confidence': '未認識・低confidenceの文字領域',
    'text_at_crop_boundary': 'crop境界に文字が接触／欠ける可能性',
    'ocr_incomplete': '原本OCRがincomplete。文字検出の見落としも保証できない',
    'no_payment_heading_observed': '会計見出しを観測できない。内容不足の可能性',
    'payment_heading_outside_crop': 'crop外にも会計見出しがあり、必要文脈が欠ける可能性',
    'multiple_tables': '複数の表候補。人間による候補選択が必要',
    'single_enclosure': '外枠のみ検出。表か単一欄か目視確認が必要',
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def detect_tables(payload):
    """Pure pixel detection. Returned rectangles use ORIGINAL pixel coordinates."""
    import cv2
    import numpy as np
    gray = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if gray is None or gray.size > 20_000_000:
        raise ValueError('invalid_image')
    h, w = gray.shape
    ink = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                               cv2.THRESH_BINARY_INV, 31, 15)
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w//30), 1)))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, h//30))))
    # A 3px detection-only connection tolerates stroke rasterization, not broken borders.
    combined = cv2.dilate(cv2.bitwise_or(horizontal, vertical), np.ones((3,3), np.uint8))
    contours, _ = cv2.findContours(combined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    tables = []
    for contour in contours:
        x, y, cw, ch = cv2.boundingRect(contour)
        if cw < max(60, w*.10) or ch < max(35, h*.035) or cw*ch < w*h*.004:
            continue
        # Find actual lines near each outer boundary; do not use an enclosing
        # bounding rectangle alone as evidence of a closed table.
        hh = horizontal[y:y+ch, x:x+cw] > 0
        vv = vertical[y:y+ch, x:x+cw] > 0
        band = min(8, max(3, min(cw,ch)//15))
        covers = [hh[:band].any(axis=0).mean(), hh[-band:].any(axis=0).mean(),
                  vv[:,:band].any(axis=1).mean(), vv[:,-band:].any(axis=1).mean()]
        if min(covers) < .70:
            continue
        junctions = cv2.bitwise_and(cv2.dilate(horizontal[y:y+ch,x:x+cw], np.ones((3,3),np.uint8)),
                                   cv2.dilate(vertical[y:y+ch,x:x+cw], np.ones((3,3),np.uint8)))
        count = cv2.connectedComponents(junctions)[0] - 1
        if count < 4:
            continue
        internal = bool((hh[band:-band].mean(axis=1) > .60).any()
                        or (vv[:,band:-band].mean(axis=0) > .60).any())
        tables.append({'box': [x,y,x+cw,y+ch], 'junctions': count,
                       'edge_coverage_min': round(float(min(covers)),3), 'internal_rules':internal})
    return sorted(tables, key=lambda t: (t['box'][1],t['box'][0]))


def detect_in_runtime(payload, executable):
    root = str(Path(__file__).resolve().parents[1])
    bootstrap = ('import sys;sys.path.insert(0,' + repr(root) + ');'
                 'from scripts.medical_table_crop_local import detection_worker;detection_worker()')
    result = subprocess.run([executable,'-I','-B','-c',bootstrap],input=payload,
                            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=45,
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:
        raise ValueError('table_detection_failed')
    return json.loads(result.stdout)


def detection_worker():
    with offline_guard():
        boxes = detect_tables(sys.stdin.buffer.read(20*1024*1024+1))
    sys.stdout.write(json.dumps(boxes))


def intersection(a,b):
    return max(0,min(a[2],b[2])-max(a[0],b[0])) * max(0,min(a[3],b[3])-max(a[1],b[1]))


def region_box(region):
    if region.bbox is None: return None
    x,y,w,h = region.bbox
    return [math.floor(x),math.floor(y),math.ceil(x+w),math.ceil(y+h)]


def inspect_crop(box, observation):
    warnings=[]; regions=[]; inside_headings=0; outside_headings=0
    for region in observation.regions:
        bounds=region_box(region)
        if bounds is None: continue
        text=unicodedata.normalize('NFKC',region.text).replace(' ','')
        heading=any(word in text for word in PAYMENT)
        if not intersection(box,bounds):
            outside_headings += int(heading)
            continue
        inside_headings += int(heading)
        reasons=[]
        if any(word in text for word in PRIVATE): reasons.append('private_or_clinical_suspected')
        if region.confidence is None or region.confidence < .90 or region.issues:
            reasons.append('unrecognized_or_low_confidence')
        if bounds[0] <= box[0]+2 or bounds[1] <= box[1]+2 or bounds[2] >= box[2]-2 or bounds[3] >= box[3]-2:
            reasons.append('text_at_crop_boundary')
        if reasons:
            regions.append({'ordinal':region.ordinal,'box':bounds,'reasons':reasons})
            warnings.extend(reasons)
    if not observation.complete: warnings.append('ocr_incomplete')
    if not inside_headings: warnings.append('no_payment_heading_observed')
    if outside_headings: warnings.append('payment_heading_outside_crop')
    return {'warnings':sorted(set(warnings)),'regions':regions,'heading_count':inside_headings,
            'outside_heading_count':outside_headings,'privacy_status':'unverified',
            'ocr_nondetection_is_not_absence':True,'qr_barcode_status':'not_inspected',
            'transmission_authorized':False,'production_authorized':False}


def rank_tables(tables, observation):
    candidates=[]
    for table in tables:
        check=inspect_crop(table['box'],observation)
        if not table['internal_rules']: check['warnings'].append('single_enclosure')
        if len(tables)>1: check['warnings'].append('multiple_tables')
        candidates.append({**table,'inspection':check})
    candidates.sort(key=lambda c:(-c['inspection']['heading_count'],
                                  'private_or_clinical_suspected' in c['inspection']['warnings'],
                                  c['box'][1],c['box'][0]))
    # A ranking is not automatic selection when more than one table exists.
    return candidates[:3], ('single_table_provisional' if len(tables)==1 else
                           'human_selection_required' if tables else 'no_closed_table')


def png(image):
    fresh=Image.new('RGB',image.size); fresh.paste(image.convert('RGB'))
    result=BytesIO(); fresh.save(result,format='PNG'); return result.getvalue()


def source_payload(card):
    path=Path(card['source_path'])
    if sha(path.read_bytes())!=card['source_sha256']: raise ValueError('source_binding_mismatch')
    for page,payload in enumerate(_images(path),1):
        if page==card['page']:
            if sha(payload)!=card['image_sha256']: raise ValueError('page_binding_mismatch')
            return payload
    raise ValueError('page_missing')


def original_rectangle(rect, turns, size):
    # Invert the exact quarter-turn operation used by the manual crop tool.
    width,height=size
    sizes=[]
    for _ in range(turns): sizes.append((width,height)); width,height=height,width
    left,top,right,bottom=rect
    for width,height in reversed(sizes):
        left,top,right,bottom=width-bottom,left,width-top,right
    return [left,top,right,bottom]


def manual_comparison(record, manual, manual_root, output):
    if any(record[k]!=manual[k] for k in ('unit','page','source_sha256','image_sha256')):
        raise ValueError('manual_source_binding_mismatch')
    expected=f'unit-{record["unit"]:02d}.png'
    if manual['image_file']!=expected: raise ValueError('manual_filename_mismatch')
    payload=(manual_root/expected).read_bytes()
    if sha(payload)!=manual['prepared_sha256']: raise ValueError('manual_image_changed')
    with Image.open(BytesIO(payload)) as image: (output/f'manual-{expected}').write_bytes(png(image))
    manual_box=original_rectangle(manual['crop'],manual['quarter_turns_ccw'],record['source_size'])
    masks=[original_rectangle(box,manual['quarter_turns_ccw'],record['source_size']) for box in manual['masks']]
    for candidate in record['candidates']:
        box=candidate['box']; overlap=intersection(box,manual_box)
        area=(manual_box[2]-manual_box[0])*(manual_box[3]-manual_box[1])
        candidate['manual_comparison']={'manual_rect_covered_fraction':round(overlap/area,3),
            'intersecting_manual_masks':sum(intersection(box,mask)>0 for mask in masks),
            'content_preservation':'unreviewed','unnecessary_information':'unreviewed',
            'no_correction_usable':'unreviewed','not_a_success_metric':True}
    record['manual_image']=f'manual-{expected}'
    record['manual_quarter_turns_ccw']=manual['quarter_turns_ccw']


def report_html(records):
    escape=html.escape
    parts=['<!doctype html><html lang="ja"><meta charset="utf-8">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\'; style-src \'unsafe-inline\'; script-src \'none\'; connect-src \'none\'; font-src \'none\'; base-uri \'none\'; form-action \'none\'">',
        '<title>Medical 10件・自動表切出しのローカル比較</title>',
        '<style>body{font-family:system-ui;margin:24px;background:#eee;color:#222}section{background:white;padding:20px;margin:24px 0} .grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}img{max-width:100%;border:1px solid #aaa} .warn{color:#a22}details{margin:12px 0}a{color:#146} @media(max-width:800px){.grid{grid-template-columns:1fr}}</style>',
        '<h1>原本 → 罫線表候補 → 手動加工画像</h1>',
        '<p>ローカル専用・外部送信なし。画像をクリックすると原寸表示。自動cropの画素は原画像からそのまま切出しています。</p>',
        '<p class="warn">全件：内容保持・不要情報混入・無修正利用可否は目視未確認。OCR検出なしは個人情報なしを意味しません。QR・バーコードとOCRの文字検出見落としは未検査です。</p>',
        '<p>青枠＝候補位置、赤枠＝OCR注意箇所。下のcropにはこれらの線は描いていません。手動画像は比較対象であり正解保証ではありません。矩形一致率は成功判定ではありません。</p>',
        '<p>確認はUnitごとに「候補番号／必要情報：残る・不足・不明／不要情報：あり・なし・不明／必要作業：なし・範囲修正・遮蔽・候補選択・保留」をまとめてください。金額の再入力は不要。確認時間は未測定です。</p>']
    for record in records:
        unit=record['unit']
        parts.append(f'<section id="unit-{unit}"><h2>Unit {unit} / page {record["page"]}</h2>')
        parts.append(f'<p>表検出 {record["detected_count"]}件、提示 {len(record["candidates"])}件、選択状態：{escape(record["selection"])}。上位3候補まで提示。</p>')
        if not record['candidates']: parts.append('<p class="warn">閉じた表候補なし。枠外総額・罫線欠け・枠なし・傾き等は未対応。原本全体の送信用fallbackは作成していません。</p>')
        for index,candidate in enumerate(record['candidates'],1):
            parts.append(f'<h3>候補 {index}</h3><div class="grid">')
            for title,name in [('原本上の候補位置と注意箇所',candidate['overlay_file']),
                               ('自動crop（未確認）',candidate['image_file']),('手動比較画像',record['manual_image'])]:
                parts.append(f'<div><p>{title}</p><a href="{escape(name)}"><img loading="lazy" src="{escape(name)}" alt="{title}"></a></div>')
            parts.append('</div><ul>')
            for code in candidate['inspection']['warnings']:
                parts.append(f'<li class="warn">{WARNINGS[code]}</li>')
            if not candidate['inspection']['warnings']: parts.append('<li>OCRの検査項目では注意検出なし。安全確認済みではありません。</li>')
            comparison=candidate['manual_comparison']
            parts.append(f'</ul><p>原本座標 {candidate["box"]}。手動矩形の包含率 {comparison["manual_rect_covered_fraction"]}、手動マスクとの交差 {comparison["intersecting_manual_masks"]}箇所。交差は混入確定ではありません。</p>')
        if not record['candidates']:
            parts.append(f'<div class="grid"><div><p>原本・候補なし</p><a href="source-{unit:02d}.png"><img src="source-{unit:02d}.png"></a></div><div><p>自動cropなし</p></div><div><p>手動比較</p><a href="{record["manual_image"]}"><img src="{record["manual_image"]}"></a></div></div>')
        parts.append('<p>目視結果：未確認。範囲修正・遮蔽・選択の必要性：未確認。確認／修正時間：未測定。</p></section>')
    return '\n'.join(parts)+ '</html>'


def evaluate(session_path, manual_root, runtime_root, output):
    output=output.resolve()
    local=Path(os.environ['LOCALAPPDATA']).resolve()
    if not output.is_relative_to(local) or output==local: raise ValueError('local_output_required')
    session_bytes=session_path.read_bytes()
    cards=source_cards(json.loads(session_bytes))
    # Source identity only is retained; no candidate or human amounts enter selection.
    output.mkdir(parents=True,exist_ok=False)
    model_manifest,executable=_runtime(runtime_root.resolve())
    adapter=RapidOcrShadowAdapter(model_manifest,python_executable=executable)
    records=[]; started=time.monotonic()
    for card in cards:
        start=time.monotonic(); unit=card['unit']; payload=source_payload(card)
        with Image.open(BytesIO(payload)) as original: source=original.convert('RGB')
        tables=detect_in_runtime(payload,executable)
        observation=adapter.observe(ReceiptImage(f'table-eval-{unit}',card['page'],payload))
        candidates,selection=rank_tables(tables,observation)
        (output/f'source-{unit:02d}.png').write_bytes(png(source))
        for index,candidate in enumerate(candidates,1):
            name=f'unit-{unit:02d}-table-{index}.png'
            crop_bytes=png(source.crop(candidate['box']))
            (output/name).write_bytes(crop_bytes)
            candidate.update(image_file=name,image_sha256=sha(crop_bytes))
            overlay=source.copy(); draw=ImageDraw.Draw(overlay)
            draw.rectangle(candidate['box'],outline='blue',width=max(2,source.width//400))
            for warning in candidate['inspection']['regions']:
                draw.rectangle(warning['box'],outline='red',width=max(2,source.width//500))
            overlay_name=f'unit-{unit:02d}-position-{index}.png'
            (output/overlay_name).write_bytes(png(overlay)); candidate['overlay_file']=overlay_name
        records.append({**card,'source_size':list(source.size),'detected_count':len(tables),
            'selection':selection,'candidates':candidates,'ocr_complete':observation.complete,
            'processing_seconds':round(time.monotonic()-start,3),'human_review':'unreviewed',
            'human_correction_seconds':None,'transmission_authorized':False})
        print(json.dumps({'unit':unit,'tables':len(tables),'presented':len(candidates),'selection':selection}),flush=True)
    # Freeze automatic decisions before opening ANY manual crop manifest/image.
    frozen=json.dumps(records,sort_keys=True,ensure_ascii=False).encode('utf-8')
    (output/'automatic-results.json').write_bytes(frozen)
    manual_bytes=(manual_root/'manifest.json').read_bytes()
    manual=json.loads(manual_bytes)['records']
    if sorted(r['unit'] for r in manual)!=list(range(1,11)): raise ValueError('manual_unit_mapping_invalid')
    by_unit={r['unit']:r for r in manual}
    for record in records: manual_comparison(record,by_unit[record['unit']],manual_root,output)
    summary={'schema_version':VERSION,'records':records,'automatic_results_sha256':sha(frozen),
        'total_seconds':round(time.monotonic()-started,3),'units_with_candidates':sum(bool(r['candidates']) for r in records),
        'single_table_provisional':sum(r['selection']=='single_table_provisional' for r in records),
        'human_selection_required':sum(r['selection']=='human_selection_required' for r in records),
        'no_candidate':sum(not r['candidates'] for r in records),'human_unreviewed':10,
        'verified_usable_without_correction':None,'verified_correction_needed':None,
        'external_http':0,'production_writes':0,'drive_sheets_writes':0,
        'ocr_source':'existing_offline_RapidOCR_single_original_pass',
        'manual_is_comparison_only':True,'qr_barcode':'not_inspected'}
    if session_path.read_bytes()!=session_bytes or (manual_root/'manifest.json').read_bytes()!=manual_bytes:
        raise ValueError('reference_changed_during_evaluation')
    for card in cards:
        if sha(Path(card['source_path']).read_bytes())!=card['source_sha256']: raise ValueError('original_changed')
    (output/'results.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'index.html').write_text(report_html(records),encoding='utf-8')
    print(json.dumps({k:v for k,v in summary.items() if k!='records'}),flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('session',type=Path); parser.add_argument('manual_root',type=Path)
    parser.add_argument('runtime_root',type=Path); parser.add_argument('output',type=Path)
    args=parser.parse_args()
    with offline_guard():
        evaluate(args.session,args.manual_root,args.runtime_root,args.output)


if __name__=='__main__': main()
