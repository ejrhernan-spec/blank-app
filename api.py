import io
import json
import logging
import os
import re
import time
from typing import Dict, List, Optional
import uuid
from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile
from google import genai
from google.genai import types
import pandas as pd
from pydantic import BaseModel, Field
import pypdf
import toml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[logging.FileHandler("audit_engine.log"), logging.StreamHandler()],
)
logger = logging.getLogger("AP_Reconciliation_Engine")

secrets_path = os.path.join(".streamlit", "secrets.toml")
api_key = None
if os.path.exists(secrets_path):
  try:
    secrets = toml.load(secrets_path)
    api_key = secrets.get("GEMINI_API_KEY")
  except Exception as e:
    logger.warning(f"Could not parse secrets.toml: {e}")

if not api_key:
  api_key = os.environ.get("GEMINI_API_KEY")

app = FastAPI(
    title="Autonomous AP Reconciliation API",
    version="1.4.0",
    description="Multi-Modal 3-Way Matching Engine with Vendor Dispute Agent",
)


class LineItem(BaseModel):
  item_sku: Optional[str] = Field(default="N/A")
  description: str
  quantity: float = Field(default=1.0)
  unit_price: float
  total_line_amount: float


class InvoiceRecord(BaseModel):
  invoice_number: str = Field(default="UNKNOWN")
  vendor_name: str
  invoice_date: str
  po_number: Optional[str] = Field(default="None")
  line_items: List[LineItem] = Field(default_factory=list)
  subtotal: float
  tax_amount: float = Field(default=0.0)
  total_amount: float


class ProvisionRequest(BaseModel):
  vendor_name: str
  billed_amount: float
  po_number: Optional[str] = None
  item_description: Optional[str] = "Approved Corporate Services"
  quantity: Optional[float] = 1.0


class OverrideRequest(BaseModel):
  invoice_number: str
  vendor_name: str
  approved_amount: float
  override_reason: str = "Auditor Executive Discretion"


class DisputeRequest(BaseModel):
  vendor_name: str
  invoice_number: str
  po_number: str
  billed_amount: float
  authorized_amount: float
  discrepancy_details: str


job_store: Dict[str, Dict] = {}

VERIFIED_VENDOR_DIRECTORY = [
    "vertex consulting group",
    "staples",
    "dell technologies",
    "amazon business",
    "fedex",
    "microsoft",
    "apple",
    "hilton",
    "marriott",
    "mckinsey & company",
    "deloitte",
]


def check_pdf_text_layer(file_bytes: bytes) -> str:
  try:
    reader = pypdf.PdfReader(io.BytesIO(file_bytes))
    return "".join(page.extract_text() or "" for page in reader.pages).strip()
  except Exception as e:
    logger.error(f"Error inspecting PDF text layer: {e}")
    return ""


def local_heuristic_extraction(raw_text: str, filename: str) -> dict:
  # Vendor: look for the label "Vendor:" and take whatever follows it.
  vendor_match = re.search(r"Vendor:\s*(.+)", raw_text)
  vendor = vendor_match.group(1).strip() if vendor_match else None

  # Total: find every dollar amount like $1,250.00 and take the largest.
  amounts = re.findall(r"\$\s?([0-9,]+\.\d{2})", raw_text)
  total_val = None
  if amounts:
    total_val = max(float(a.replace(",", "")) for a in amounts)

  # PO number: look for something like PO-1002.
  po_match = re.search(r"PO[-\s]?\d+", raw_text, re.IGNORECASE)
  po_num = po_match.group(0).replace(" ", "-").upper() if po_match else None

  # Invoice number: only take what comes AFTER a label like "Invoice Number:".
  inv_match = re.search(
      r"Invoice\s*(?:Number|No\.?|#)\s*:?\s*([A-Za-z0-9\-]+)",
      raw_text,
      re.IGNORECASE,
  )
  inv_num = inv_match.group(1) if inv_match else None

  # Date: look for a date written like 2026-10-22.
  date_match = re.search(r"\d{4}-\d{2}-\d{2}", raw_text)
  inv_date = date_match.group(0) if date_match else None

  return {
      "invoice_number": inv_num,
      "vendor_name": vendor,
      "invoice_date": inv_date,
      "po_number": po_num,
      "line_items": [],
      "subtotal": total_val,
      "tax_amount": 0.0,
      "total_amount": total_val,
  }


def provision_po_and_grn_to_ledger(
    vendor: str,
    amount: float,
    po_num: Optional[str] = None,
    description: str = "Authorized Expense",
    qty: float = 1.0,
) -> str:
  clean_vendor = vendor.strip()

  with pd.ExcelFile("company_ledger.xlsx") as xls:
    pos_df = pd.read_excel(xls, "Purchase_Orders")
    grns_df = pd.read_excel(xls, "Goods_Received")
    history_df = pd.read_excel(xls, "Invoice_History")

  if not po_num or po_num in ["None", "UNKNOWN", ""]:
    existing_pos = pos_df["PO_Number"].dropna().tolist()
    next_id = len(existing_pos) + 1001
    po_num = f"PO-{next_id}"

  unit_price = round(amount / qty, 2) if qty > 0 else amount

  if po_num in pos_df["PO_Number"].values:
    pos_df.loc[pos_df["PO_Number"] == po_num, "Authorized_Total"] = amount
    pos_df.loc[pos_df["PO_Number"] == po_num, "Authorized_UnitPrice"] = unit_price
  else:
    new_po = pd.DataFrame([{
        "PO_Number": po_num,
        "Vendor": clean_vendor,
        "Item_SKU": "GEN-01",
        "Description": description,
        "Authorized_Qty": qty,
        "Authorized_UnitPrice": unit_price,
        "Authorized_Total": amount,
    }])
    pos_df = pd.concat([pos_df, new_po], ignore_index=True)

    grn_id = f"GRN-{po_num.replace('PO-', '')}"
    new_grn = pd.DataFrame([{
        "GRN_Number": grn_id,
        "PO_Number": po_num,
        "Item_SKU": "GEN-01",
        "Received_Qty": qty,
        "Receipt_Date": "2026-10-22",
    }])
    grns_df = pd.concat([grns_df, new_grn], ignore_index=True)

  with pd.ExcelWriter("company_ledger.xlsx") as writer:
    pos_df.to_excel(writer, sheet_name="Purchase_Orders", index=False)
    grns_df.to_excel(writer, sheet_name="Goods_Received", index=False)
    history_df.to_excel(writer, sheet_name="Invoice_History", index=False)

  logger.info(f"Updated/Provisioned {po_num} for {clean_vendor} (${amount:,.2f})")
  return po_num


def reconcile_3_way(
    extracted: dict,
    variance_tolerance: float = 0.05,
    auto_provision_legit_vendors: bool = False,
) -> dict:
  with pd.ExcelFile("company_ledger.xlsx") as xls:
    pos_df = pd.read_excel(xls, "Purchase_Orders")
    grns_df = pd.read_excel(xls, "Goods_Received")
    history_df = pd.read_excel(xls, "Invoice_History")

  inv_num = extracted.get("invoice_number", "UNKNOWN")
  po = extracted.get("po_number")
  raw_vendor = extracted.get("vendor_name", "").strip()
  vendor_lower = raw_vendor.lower()
  billed_amt = extracted.get("total_amount", 0.0)

  if not history_df.empty and inv_num != "UNKNOWN":
    dupes = history_df[
        history_df["Invoice_Number"].astype(str).str.strip().str.lower()
        == inv_num.strip().lower()
    ]
    if not dupes.empty:
      return {
          "status": "FLAGGED",
          "verdict": "DUPLICATE_INVOICE",
          "details": f"Invoice {inv_num} already processed in past payment batch.",
      }

  matched_po = (
      pos_df[pos_df["PO_Number"] == po]
      if po and po != "None"
      else pd.DataFrame()
  )
  if matched_po.empty and vendor_lower:
    matched_po = pos_df[
        pos_df["Vendor"].str.strip().str.lower() == vendor_lower
    ]

  if matched_po.empty:
    return {
        "status": "FLAGGED",
        "verdict": "UNAPPROVED_PO",
        "details": f"Vendor '{raw_vendor}' has no authorized PO in the system.",
        "linked_po": "None",
        "authorized_amount": 0.0,
    }

  po_num = matched_po.iloc[0]["PO_Number"]
  approved_amt = matched_po["Authorized_Total"].sum()
  price_diff = billed_amt - approved_amt

  matched_grn = grns_df[grns_df["PO_Number"] == po_num]
  flags = []

  if abs(price_diff) > variance_tolerance:
    flags.append(
        f"Price Variance: Billed ${billed_amt:,.2f} vs PO ${approved_amt:,.2f} (Diff: ${price_diff:+,.2f})"
    )

  if matched_grn.empty:
    flags.append(f"Warehouse Receipt Missing: No GRN recorded for {po_num}.")
  else:
    total_authorized_qty = matched_po["Authorized_Qty"].sum()
    total_received_qty = matched_grn["Received_Qty"].sum()
    if total_received_qty < total_authorized_qty:
      flags.append(
          f"Quantity Short-Shipment: Warehouse received {total_received_qty} of {total_authorized_qty} units."
      )

  if flags:
    return {
        "status": "FLAGGED",
        "verdict": "3_WAY_DISCREPANCY",
        "details": " | ".join(flags),
        "linked_po": po_num,
        "authorized_amount": approved_amt,
    }

  return {
      "status": "APPROVED",
      "verdict": "MATCH_SUCCESSFUL",
      "details": "Invoice, PO, and GRN all reconciled successfully.",
      "linked_po": po_num,
      "authorized_amount": approved_amt,
  }


def execute_extraction(file_bytes: bytes, mime_type: str, filename: str) -> dict:
  if api_key:
    client = genai.Client(api_key=api_key)
    document_part = types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
    prompt = (
        "Extract structured invoice metadata, line items, and totals matching"
        " schema."
    )

    for model_name in [
        "gemini-2.0-flash",
        "gemini-flash-latest",
        "gemini-3.8-flash",
    ]:
      try:
        response = client.models.generate_content(
            model=model_name,
            contents=[document_part, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=InvoiceRecord,
            ),
        )
        if response and response.text:
          return json.loads(response.text)
      except Exception:
        continue

  text_layer = check_pdf_text_layer(file_bytes)
  return local_heuristic_extraction(text_layer, filename)


# -----------------------------------------------------------------------------
# API Endpoints
# -----------------------------------------------------------------------------
@app.get("/health")
def health_check():
  return {
      "status": "online",
      "engine": "gemini-3.8-flash (with local fallback)",
      "agent": "active",
  }


@app.post("/api/v1/ledger/override")
def override_invoice(req: OverrideRequest):
  with pd.ExcelFile("company_ledger.xlsx") as xls:
    pos_df = pd.read_excel(xls, "Purchase_Orders")
    grns_df = pd.read_excel(xls, "Goods_Received")
    history_df = pd.read_excel(xls, "Invoice_History")

  new_record = pd.DataFrame([{
      "Invoice_Number": req.invoice_number,
      "Vendor": req.vendor_name,
      "Billed_Total": req.approved_amount,
      "Payment_Status": f"Approved (Override: {req.override_reason})",
  }])
  history_df = pd.concat([history_df, new_record], ignore_index=True)

  with pd.ExcelWriter("company_ledger.xlsx") as writer:
    pos_df.to_excel(writer, sheet_name="Purchase_Orders", index=False)
    grns_df.to_excel(writer, sheet_name="Goods_Received", index=False)
    history_df.to_excel(writer, sheet_name="Invoice_History", index=False)

  logger.info(
      f"Auditor Override: Approved {req.invoice_number} for {req.vendor_name}"
  )
  return {"status": "SUCCESS", "message": f"Invoice {req.invoice_number} recorded as Approved."}


@app.post("/api/v1/ledger/provision")
def manual_provision_po(req: ProvisionRequest):
  po_created = provision_po_and_grn_to_ledger(
      vendor=req.vendor_name,
      amount=req.billed_amount,
      po_num=req.po_number,
      description=req.item_description or "Manual Approved PO",
      qty=req.quantity or 1.0,
  )
  return {
      "status": "SUCCESS",
      "message": f"PO {po_created} successfully written to company_ledger.xlsx",
      "po_number": po_created,
  }


@app.post("/api/v1/agent/dispute-email")
def generate_dispute_email(req: DisputeRequest):
  """Generates a professional supplier dispute communication."""
  subject = f"Audit Notice: Discrepancy on Invoice {req.invoice_number} (Ref: {req.po_number})"
  body = (
      f"Dear Accounts Receivable Team at {req.vendor_name},\n\n"
      f"During our automated Accounts Payable 3-way reconciliation audit for Invoice {req.invoice_number} "
      f"(associated with Purchase Order {req.po_number}), our internal controls flagged the following discrepancies:\n\n"
      f"• Audit Finding: {req.discrepancy_details}\n"
      f"• Total Billed: ${req.billed_amount:,.2f}\n"
      f"• Authorized Baseline: ${req.authorized_amount:,.2f}\n\n"
      f"Pursuant to corporate financial compliance guidelines, payment for this invoice is temporarily on audit hold. "
      f"Please submit an adjusted invoice reflecting authorized PO pricing, or provide proof of warehouse delivery for the "
      f"short-shipped quantities.\n\n"
      f"Best regards,\n"
      f"Accounts Payable Audit Department\n"
      f"Internal Controls & Compliance Office"
  )
  return {"subject": subject, "body": body}


def background_batch_worker(job_id: str, files_data: list):
  results = []
  for filename, file_bytes, mime_type in files_data:
    try:
      extracted = execute_extraction(file_bytes, mime_type, filename)
      audit_result = reconcile_3_way(extracted)
      results.append({
          "filename": filename,
          "status": "SUCCESS",
          "extracted": extracted,
          "audit": audit_result,
      })
    except Exception as e:
      results.append({"filename": filename, "status": "FAILED", "error": str(e)})

  job_store[job_id]["status"] = "COMPLETED"
  job_store[job_id]["results"] = results


@app.post("/api/v1/invoices/batch-audit")
async def audit_batch_invoices(
    files: List[UploadFile], background_tasks: BackgroundTasks
):
  job_id = str(uuid.uuid4())
  files_data = []
  for f in files:
    content = await f.read()
    files_data.append((f.filename, content, f.content_type or "application/pdf"))

  job_store[job_id] = {
      "status": "PROCESSING",
      "total_files": len(files),
      "results": [],
  }
  background_tasks.add_task(background_batch_worker, job_id, files_data)
  return {"job_id": job_id, "status": "QUEUED"}


@app.get("/api/v1/invoices/jobs/{job_id}")
def get_job_status(job_id: str):
  job = job_store.get(job_id)
  if not job:
    raise HTTPException(status_code=404, detail="Job ID not found")
  return job 
