"""Operator-selected accounting crops. Local Tk window; no AI or network I/O."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from io import BytesIO
from pathlib import Path
import time

from PIL import Image, ImageDraw
from scripts.evaluate_medical_ai_structured_offline import _images


def source_cards(session):
    # Deliberately discard review answers and machine candidates.
    fields = ('unit', 'page', 'source_path', 'source_sha256', 'image_sha256')
    cards = [{key: row[key] for key in fields} for row in session['units']]
    if len(cards) != 10 or sorted(c['unit'] for c in cards) != list(range(1, 11)):
        raise ValueError('invalid_unit_mapping')
    return sorted(cards, key=lambda c: c['unit'])


def load_source(card):
    path = Path(card['source_path'])
    if hashlib.sha256(path.read_bytes()).hexdigest() != card['source_sha256']:
        raise ValueError('source_changed')
    for page, payload in enumerate(_images(path), 1):
        if page == card['page']:
            if hashlib.sha256(payload).hexdigest() != card['image_sha256']:
                raise ValueError('page_binding_changed')
            with Image.open(BytesIO(payload)) as image:
                return image.convert('RGB')
    raise ValueError('page_missing')


def render(image, crop, masks):
    width, height = image.size
    def valid(box):
        return len(box) == 4 and all(type(v) is int for v in box) and 0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height
    if not valid(crop) or any(not valid(box) for box in masks):
        raise ValueError('invalid_rectangle')
    clean = Image.new('RGB', image.size, 'white')
    clean.paste(image.convert('RGB'))
    draw = ImageDraw.Draw(clean)
    for left, top, right, bottom in masks:
        draw.rectangle((left, top, right - 1, bottom - 1), fill='white')
    result = clean.crop(crop)
    output = BytesIO()
    result.save(output, format='PNG')
    return output.getvalue()


class Editor:
    def __init__(self, root, cards, output):
        import tkinter as tk
        from PIL import ImageTk
        self.tk, self.ImageTk = tk, ImageTk
        self.root, self.cards, self.output = root, cards, output
        self.index, self.records = 0, []
        self.mode = tk.StringVar(value='crop')
        root.title('Medical 会計欄の切出し — ローカルのみ／送信しません')
        self.status = tk.StringVar()
        tk.Label(root, textvariable=self.status).pack()
        tk.Label(root, text='会計欄全体をドラッグ → マスクで個人情報をドラッグ → プレビュー → 確認して保存。正解金額ではなく欄の文脈を残してください。').pack()
        bar = tk.Frame(root); bar.pack()
        for label, mode in [('会計欄を選択', 'crop'), ('個人情報をマスク', 'mask')]:
            tk.Radiobutton(bar, text=label, variable=self.mode, value=mode).pack(side='left')
        for label, command in [('マスクを1つ戻す', self.undo), ('左へ90度回転', self.rotate), ('プレビュー', self.preview)]:
            tk.Button(bar, text=label, command=command).pack(side='left')
        self.canvas = tk.Canvas(root, width=1100, height=700, bg='#777')
        self.canvas.pack()
        self.canvas.bind('<ButtonPress-1>', self.down)
        self.canvas.bind('<B1-Motion>', self.drag)
        self.canvas.bind('<ButtonRelease-1>', self.up)
        self.load()

    def load(self):
        if self.index == len(self.cards):
            self.canvas.delete('all')
            self.status.set('10件保存完了。加工画像の一括送信承認は別途必要です。このウィンドウを閉じてください。')
            self.image = None
            return
        self.image = load_source(self.cards[self.index])
        self.crop, self.masks, self.turns = None, [], 0
        self.started = time.monotonic()
        self.status.set(f'Unit {self.cards[self.index]["unit"]} / 原本 page {self.cards[self.index]["page"]} — 未保存')
        self.paint()

    def paint(self):
        if self.image is None: return
        self.scale = min(1100 / self.image.width, 700 / self.image.height)
        size = (max(1, round(self.image.width * self.scale)), max(1, round(self.image.height * self.scale)))
        self.photo = self.ImageTk.PhotoImage(self.image.resize(size))
        self.canvas.delete('all'); self.canvas.create_image(0, 0, image=self.photo, anchor='nw')
        if self.crop:
            self.canvas.create_rectangle(*(v * self.scale for v in self.crop), outline='blue', width=3)
        for box in self.masks:
            self.canvas.create_rectangle(*(v * self.scale for v in box), fill='white', outline='red')

    def down(self, event):
        if self.image is not None: self.start = (event.x, event.y)

    def drag(self, event):
        if self.image is None or not hasattr(self, 'start'): return
        self.canvas.delete('drag')
        self.canvas.create_rectangle(*self.start, event.x, event.y, outline='orange', width=2, tags='drag')

    def up(self, event):
        if self.image is None or not hasattr(self, 'start'): return
        x, y = self.start; del self.start
        box = (max(0, math.floor(min(x,event.x)/self.scale)), max(0,math.floor(min(y,event.y)/self.scale)),
               min(self.image.width,math.ceil(max(x,event.x)/self.scale)), min(self.image.height,math.ceil(max(y,event.y)/self.scale)))
        if box[0] < box[2] and box[1] < box[3]:
            if self.mode.get() == 'crop': self.crop = box
            else: self.masks.append(box)
        self.paint()

    def undo(self):
        if self.masks: self.masks.pop(); self.paint()

    def rotate(self):
        if self.image is None: return
        self.image = self.image.transpose(Image.Transpose.ROTATE_90)
        self.turns = (self.turns + 1) % 4
        self.crop, self.masks = None, []
        self.paint()

    def preview(self):
        if self.image is None or self.crop is None: return
        payload = render(self.image, self.crop, self.masks)
        window = self.tk.Toplevel(self.root); window.title('送信候補の確認（ローカル表示）')
        window.grab_set()
        with Image.open(BytesIO(payload)) as image:
            image.thumbnail((1100,700)); photo = self.ImageTk.PhotoImage(image)
        label = self.tk.Label(window, image=photo); label.image = photo; label.pack()
        checked = self.tk.BooleanVar(value=False)
        self.tk.Checkbutton(window, text='会計欄の文脈を残し、不要な個人情報が見えていないことを確認した', variable=checked).pack()
        def save():
            if not checked.get(): return
            card = self.cards[self.index]
            name = f'unit-{card["unit"]:02d}.png'
            with (self.output / name).open('xb') as handle: handle.write(payload)
            self.records.append({**card, 'image_file': name, 'prepared_sha256': hashlib.sha256(payload).hexdigest(),
                'crop': self.crop, 'masks': self.masks, 'quarter_turns_ccw': self.turns,
                'manual_crop_used': True, 'local_visual_checked': True, 'preparation_seconds': round(time.monotonic()-self.started,3),
                'external_transmission_approved': False})
            temp = self.output / 'manifest.tmp'
            temp.write_text(json.dumps({'schema_version':'medical-manual-crop-v1', 'records':self.records,
                'prepared_count':len(self.records), 'external_requests':0}, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(self.output / 'manifest.json')
            window.destroy(); self.index += 1; self.load()
        self.tk.Button(window, text='ローカル保存して次へ（送信しません）', command=save).pack()
        self.tk.Button(window, text='戻って修正', command=window.destroy).pack()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('session', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    # Require a local non-repository destination; never write into source corpus.
    import os
    local = Path(os.environ['LOCALAPPDATA']).resolve()
    output = args.output.resolve()
    if not output.is_relative_to(local) or output == local:
        raise ValueError('output_requires_LocalAppData_subdirectory')
    cards = source_cards(json.loads(args.session.read_text(encoding='utf-8')))
    output.mkdir(parents=True, exist_ok=False)
    import tkinter as tk
    root = tk.Tk(); Editor(root, cards, output); root.mainloop()


if __name__ == '__main__': main()
