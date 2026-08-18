from __future__ import annotations
import csv, hashlib, re, unicodedata
from datetime import datetime
from .sheets import SheetsDB
from .utils import canonical_hash, now_jst_string

def norm_text(s:str)->str:
    return unicodedata.normalize("NFKC", str(s or "")).strip()

def parse_money(s)->int:
    t=norm_text(s).replace(",","").replace("円","")
    return int(t) if t else 0

def parse_aupay_card_csv(path:str)->list[dict]:
    # au PAY Card exports Japanese CSV in CP932 with metadata lines before the table.
    with open(path,"r",encoding="cp932",newline="") as f:
        rows=list(csv.reader(f))
    header_i=None
    for i,r in enumerate(rows):
        if "利用日" in r and "利用店舗" in r and "利用額（円）" in r:
            header_i=i; break
    if header_i is None:
        raise ValueError("au PAYカードCSVの利用明細ヘッダーを見つけられません")
    hdr=rows[header_i]
    pos={name:hdr.index(name) for name in ["利用日","利用店舗","利用額（円）","支払い区分","ご利用者","摘要"]}
    out=[]
    for r in rows[header_i+1:]:
        if not r or (r[0].startswith("■") if r[0] else False): break
        if len(r)<=max(pos.values()) or not r[pos["利用日"]]: continue
        d={
            "date":datetime.strptime(r[pos["利用日"]],"%Y/%m/%d").strftime("%Y-%m-%d"),
            "merchant":norm_text(r[pos["利用店舗"]]),
            "amount":parse_money(r[pos["利用額（円）"]]),
            "payment_type":norm_text(r[pos["支払い区分"]]),
            "member":norm_text(r[pos["ご利用者"]]),
            "memo":norm_text(r[pos["摘要"]]),
        }
        out.append(d)
    # Stable duplicate occurrence suffix, needed when same member/date/store/amount repeats.
    seen={}
    for d in out:
        base="|".join([d["date"],d["merchant"],str(d["amount"]),d["payment_type"],d["member"],d["memo"]])
        seen[base]=seen.get(base,0)+1
        d["occurrence"]=seen[base]
        d["import_id"]="aupaycard:"+hashlib.sha256((base+"|"+str(d["occurrence"])).encode()).hexdigest()[:24]
    return out

def is_aupay_charge(merchant:str)->bool:
    m=norm_text(merchant).upper()
    return "AU PAY 残高オートチャージ" in m or "AU PAY 残高チャージ" in m

def is_amazon(merchant:str)->bool:
    return "AMAZON.CO.JP" in norm_text(merchant).upper()

class AuPayCardPipeline:
    def __init__(self,db:SheetsDB): self.db=db

    def _amazon_candidates(self):
        rows=self.db.get("Amazon注文!A2:M")
        grouped={}
        for r in rows:
            if len(r)<7: continue
            try: amt=int(float(str(r[6]).replace(",","")))
            except: continue
            order_id=str(r[1]) if len(r)>1 else ""
            if not order_id: continue
            candidate=grouped.setdefault(order_id,{
                "key":f"amazon:{order_id}","order_id":order_id,
                "date":r[3] if len(r)>3 else "","amount":0,"items":0,
            })
            candidate["amount"]+=amt
            candidate["items"]+=1
        return list(grouped.values())

    def _classify_amazon(self,date:str,amount:int,amazon:list[dict]):
        candidates=[x for x in amazon
                    if x["amount"]==amount and self._days(x["date"],date)<=7]
        if len(candidates)==1:
            return "matched_amazon",candidates
        if candidates:
            return "amazon_needs_review",candidates
        return "amazon_unmatched",candidates

    @staticmethod
    def _days(a,b):
        try:return abs((datetime.strptime(a,"%Y-%m-%d")-datetime.strptime(b,"%Y-%m-%d")).days)
        except:return 999

    def preview(self,path:str):
        tx=parse_aupay_card_csv(path)
        amazon=self._amazon_candidates()
        counts={"rows":len(tx),"aupay_charge":0,"amazon_matched":0,
                "amazon_ambiguous":0,"amazon_unmatched":0,"other":0}
        samples=[]
        for d in tx:
            if is_aupay_charge(d["merchant"]):
                state="transfer_aupay_charge"; counts["aupay_charge"]+=1
            elif is_amazon(d["merchant"]):
                state,c=self._classify_amazon(d["date"],d["amount"],amazon)
                if state=="matched_amazon":
                    counts["amazon_matched"]+=1
                elif state=="amazon_needs_review":
                    counts["amazon_ambiguous"]+=1
                else:
                    counts["amazon_unmatched"]+=1
                if len(samples)<5: samples.append({"card":d,"candidates":c[:5]})
            else:
                state="unclassified_card"; counts["other"]+=1
        return {"summary":counts,"amazon_samples":samples}

    def import_csv(self,path:str):
        return self.import_transactions(parse_aupay_card_csv(path))

    def import_transactions(self,tx:list[dict]):
        existing=self.db.import_ids()
        amazon=self._amazon_candidates()
        rows=[]; stats={"source_rows":len(tx),"new":0,"unchanged":0,"aupay_charge":0,
                        "amazon_matched":0,"amazon_needs_review":0,
                        "amazon_unmatched":0,"unclassified_card":0}
        for d in tx:
            if d["import_id"] in existing:
                stats["unchanged"]+=1; continue
            note=f"会員={d['member']}"
            if d["memo"]: note += f"; 摘要={d['memo']}"
            if is_aupay_charge(d["merchant"]):
                state="transfer_aupay_charge"; stats["aupay_charge"]+=1
                note += "; 支出計上しない（au PAY残高への資金移動）"
            elif is_amazon(d["merchant"]):
                state,c=self._classify_amazon(d["date"],d["amount"],amazon)
                stats[state]+=1
                if state=="matched_amazon":
                    note += f"; Amazonキー={c[0]['key']}; Amazon注文と照合済み・カード側は支出計上しない"
                else:
                    note += f"; Amazon候補数={len(c)}"
            else:
                state="unclassified_card"; stats["unclassified_card"]+=1
            h=canonical_hash(d)
            rows.append([d["import_id"],now_jst_string(),"au PAYカード",d["import_id"],d["date"],
                         d["merchant"],d["amount"],d["payment_type"],state,"",h,note])
            stats["new"]+=1
        self.db.append("取込データ",rows)
        return stats

    def reclassify_amazon(self):
        amazon=self._amazon_candidates()
        rows=self.db.get("取込データ!A2:L")
        updates=[]
        stats={"amazon_rows":0,"updated":0,"matched_amazon":0,
               "amazon_needs_review":0,"amazon_unmatched":0}
        for row_num,raw in enumerate(rows,start=2):
            row=list(raw)+[""]*max(0,12-len(raw)); row=row[:12]
            if row[2]!="au PAYカード" or not is_amazon(str(row[5])):
                continue
            stats["amazon_rows"]+=1
            try: amount=parse_money(row[6])
            except (TypeError,ValueError): amount=0
            state,candidates=self._classify_amazon(str(row[4]),amount,amazon)
            stats[state]+=1
            target=candidates[0]["key"] if state=="matched_amazon" else ""
            if row[8]==state and row[9]==target:
                continue
            note_parts=[part.strip() for part in str(row[11]).split(";") if part.strip()]
            note_parts=[part for part in note_parts if not (
                part.startswith("Amazonキー=")
                or part.startswith("Amazon候補数=")
                or part.startswith("Amazon注文と照合済み")
                or part.startswith("Amazon再照合=")
            )]
            note_parts.append(f"Amazon再照合={state}; 候補数={len(candidates)}")
            row[8]=state; row[9]=target; row[11]="; ".join(note_parts)
            updates.append((row_num,row))
        self.db.update_rows("取込データ",updates)
        stats["updated"]=len(updates)
        return stats
