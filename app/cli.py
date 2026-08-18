from __future__ import annotations
import argparse, csv, mimetypes, os
from .settings import Settings
from .sheets import SheetsDB
from .gemini_ai import GeminiAI
from .receipt_pipeline import ReceiptPipeline
from .amazon_pipeline import AmazonPipeline
from .drive_receipts import process_inbox
from .aupay_card_pipeline import AuPayCardPipeline
from .aupay_mail_pipeline import (
    AuPayMailPipeline,
    authorize_gmail,
    parse_eml,
    parse_aupay_card_eml,
)
from .aupay_csv_pipeline import AuPayCsvPipeline
from .reconciliation import ReconciliationPipeline
from .review_pipeline import ReviewApprovalPipeline, ReviewPipeline
from .expense_view import ExpenseViewPipeline

def load_categories(path="config/categories.tsv"):
    with open(path,encoding="utf-8") as f:
        rd=csv.reader(f,delimiter="\t"); next(rd,None); return [(r[0],r[1]) for r in rd if len(r)>=2]

def make(require_gemini=True):
    s=Settings(); s.validate(need_gemini=require_gemini,need_sheet=True)
    db=SheetsDB(s.spreadsheet_id)
    ai=GeminiAI(s.gemini_api_key,s.gemini_model) if require_gemini else None
    return s,db,ai

def main():
    p=argparse.ArgumentParser(description="家計簿AI")
    sub=p.add_subparsers(dest="cmd",required=True)
    sub.add_parser("init")
    r=sub.add_parser("receipt"); r.add_argument("image")
    an=sub.add_parser("analyze"); an.add_argument("image")
    a=sub.add_parser("amazon"); a.add_argument("csv")
    ab=sub.add_parser("amazon-baseline"); ab.add_argument("csv")
    cp=sub.add_parser("card-preview"); cp.add_argument("csv")
    ci=sub.add_parser("card-import"); ci.add_argument("csv")
    ae=sub.add_parser("aupay-eml"); ae.add_argument("eml")
    ag=sub.add_parser("aupay-gmail"); ag.add_argument("--max-results",type=int,default=100)
    acp=sub.add_parser("aupay-csv-preview"); acp.add_argument("csv")
    aci=sub.add_parser("aupay-csv-import"); aci.add_argument("csv")
    ce=sub.add_parser("card-eml-import"); ce.add_argument("eml")
    ga=sub.add_parser("gmail-authorize")
    ga.add_argument("client_json")
    ga.add_argument("token_output")
    sub.add_parser("reconcile-preview")
    sub.add_parser("reconcile")
    sub.add_parser("review-preview")
    sub.add_parser("review-refresh")
    sub.add_parser("review-apply")
    sub.add_parser("expenses-preview")
    sub.add_parser("expenses-refresh")
    sub.add_parser("drive-receipts")
    sub.add_parser("doctor")
    args=p.parse_args()
    if args.cmd=="doctor":
        import importlib.util
        checks = {
            "SPREADSHEET_ID": bool(Settings().spreadsheet_id),
            "service-account.json": os.path.exists(Settings().service_account_file) or bool(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON","").strip()),
            "google.genai": importlib.util.find_spec("google.genai") is not None,
            "googleapiclient": importlib.util.find_spec("googleapiclient") is not None,
            "pandas": importlib.util.find_spec("pandas") is not None,
        }
        for k,v in checks.items(): print(f"{'OK' if v else 'NG'}  {k}")
        if not all(checks.values()):
            raise SystemExit(1)
        print("開発環境チェック完了")
    elif args.cmd=="init":
        s,db,_=make(False); db.ensure_schema(load_categories()); print("Sheets初期化/検証完了")
    elif args.cmd=="receipt":
        s,db,ai=make(); data=open(args.image,"rb").read(); mime=mimetypes.guess_type(args.image)[0] or "image/jpeg"
        print(ReceiptPipeline(db,ai).process_bytes(data,mime,os.path.basename(args.image)))
    elif args.cmd=="analyze":
        s,db,ai=make(); data=open(args.image,"rb").read(); mime=mimetypes.guess_type(args.image)[0] or "image/jpeg"
        result=ai.analyze_receipt(data,mime,db.categories())
        print(result.model_dump())
    elif args.cmd=="amazon":
        s,db,ai=make(); print(AmazonPipeline(db,ai).import_csv(args.csv))
    elif args.cmd=="amazon-baseline":
        s,db,_=make(False); print(AmazonPipeline(db,None).import_csv(args.csv,baseline=True))
    elif args.cmd=="card-preview":
        s,db,_=make(False); print(AuPayCardPipeline(db).preview(args.csv))
    elif args.cmd=="card-import":
        s,db,_=make(False); print(AuPayCardPipeline(db).import_csv(args.csv))
    elif args.cmd=="aupay-eml":
        s,db,_=make(False); print(AuPayMailPipeline(db).import_notice(parse_eml(args.eml)))
    elif args.cmd=="aupay-gmail":
        s,db,_=make(False); s.validate(need_gmail=True)
        print(AuPayMailPipeline(db).import_gmail(s.gmail_token_json,s.aupay_gmail_query,args.max_results))
    elif args.cmd=="aupay-csv-preview":
        s,db,_=make(False); print(AuPayCsvPipeline(db).preview(args.csv))
    elif args.cmd=="aupay-csv-import":
        s,db,_=make(False); print(AuPayCsvPipeline(db).import_csv(args.csv))
    elif args.cmd=="card-eml-import":
        s,db,_=make(False)
        print(AuPayCardPipeline(db).import_transactions(parse_aupay_card_eml(args.eml)))
    elif args.cmd=="gmail-authorize":
        authorize_gmail(args.client_json,args.token_output)
        print(f"Gmail読み取り用トークンを保存しました: {args.token_output}")
    elif args.cmd=="reconcile-preview":
        s,db,_=make(False); print(ReconciliationPipeline(db).preview())
    elif args.cmd=="reconcile":
        s,db,_=make(False); print(ReconciliationPipeline(db).apply())
    elif args.cmd=="review-preview":
        s,db,_=make(False); print(ReviewPipeline(db).preview())
    elif args.cmd=="review-refresh":
        s,db,_=make(False); print(ReviewPipeline(db).refresh())
    elif args.cmd=="review-apply":
        s,db,_=make(False); print(ReviewApprovalPipeline(db).apply())
    elif args.cmd=="expenses-preview":
        s,db,_=make(False); print(ExpenseViewPipeline(db).preview())
    elif args.cmd=="expenses-refresh":
        s,db,_=make(False); print(ExpenseViewPipeline(db).refresh())
    elif args.cmd=="drive-receipts":
        s,db,ai=make(); s.validate(need_drive=True)
        for name,res in process_inbox(s.receipt_drive_folder_id,ReceiptPipeline(db,ai),s.processed_drive_folder_id): print(name,res)

if __name__=="__main__":main()
