import time
import pandas as pd
from PIL import Image
import requests
import streamlit as st

st.set_page_config(page_title="AI 3-Way AP Invoice Auditor", layout="wide")
st.title("🧾 Autonomous AP Reconciliation Dashboard")
st.caption("Decoupled Enterprise Architecture: Streamlit UI + Async FastAPI Engine")

API_BASE_URL = "http://127.0.0.1:8000"

with st.sidebar:
  st.header("⚙️ System Status")
  backend_online = False
  try:
    health_resp = requests.get(f"{API_BASE_URL}/health", timeout=2)
    if health_resp.status_code == 200:
      st.success("🟢 FastAPI Microservice: Online (Port 8000)")
      backend_online = True
  except Exception:
    st.error("🔴 FastAPI Microservice Offline (Port 8000)")

  variance_tolerance = st.slider(
      "Price Variance Tolerance ($)", 0.00, 5.00, 0.05, 0.01
  )
  st.markdown("---")
  st.subheader("Internal Database (Ledger)")

  try:
    with pd.ExcelFile("company_ledger.xlsx") as xls:
      pos_df = pd.read_excel(xls, "Purchase_Orders")
      grns_df = pd.read_excel(xls, "Goods_Received")
      history_df = pd.read_excel(xls, "Invoice_History")

    view_tab = st.radio(
        "Inspect Table:",
        ["Purchase Orders", "Goods Received (GRN)", "Invoice History"],
    )
    if view_tab == "Purchase Orders":
      st.dataframe(pos_df, hide_index=True)
    elif view_tab == "Goods Received (GRN)":
      st.dataframe(grns_df, hide_index=True)
    else:
      st.dataframe(history_df, hide_index=True)
  except Exception as e:
    st.error(f"⚠️ Error reading company_ledger.xlsx: {e}")

col1, col2 = st.columns([1, 1])

with col1:
  st.subheader("1. Batch Document Ingestion")
  uploaded_files = st.file_uploader(
      "Drop invoices here",
      type=["pdf", "png", "jpg", "jpeg", "webp"],
      accept_multiple_files=True,
  )

  if uploaded_files:
    st.info(f"📂 **{len(uploaded_files)} file(s)** staged.")
    if len(uploaded_files) == 1 and uploaded_files[0].type != "application/pdf":
      st.image(Image.open(uploaded_files[0]), use_container_width=True)

with col2:
  st.subheader("2. Audit Queue & Reconciled Verdicts")

  if uploaded_files:
    if st.button("Enqueue Batch for Audit", type="primary", disabled=not backend_online):
      files_payload = [
          ("files", (f.name, f.getvalue(), f.type or "application/pdf"))
          for f in uploaded_files
      ]
      res = requests.post(f"{API_BASE_URL}/api/v1/invoices/batch-audit", files=files_payload)

      if res.status_code == 200:
        job_id = res.json()["job_id"]
        progress_bar = st.progress(0, text="Auditing batch asynchronously...")

        while True:
          time.sleep(1.5)
          poll_resp = requests.get(f"{API_BASE_URL}/api/v1/invoices/jobs/{job_id}")
          if poll_resp.status_code == 200 and poll_resp.json().get("status") == "COMPLETED":
            st.session_state["last_audit_results"] = poll_resp.json().get("results", [])
            progress_bar.progress(100, text="Audit completed!")
            break

  if "last_audit_results" in st.session_state:
    st.markdown("### 📋 Audit Batch Results")
    for idx, item in enumerate(st.session_state["last_audit_results"]):
      filename = item.get("filename", "Unknown File")
      if item.get("status") == "FAILED":
        st.error(f"Failed to process {filename}: {item.get('error')}")
        continue

      audit = item["audit"]
      extracted = item.get("extracted", {})

      with st.expander(f"📄 {filename} — {audit.get('status')}", expanded=True):
        m1, m2, m3 = st.columns(3)
        m1.metric("Vendor", extracted.get("vendor_name", "N/A"))
        m2.metric("Invoice #", extracted.get("invoice_number", "N/A"))
        m3.metric("Billed Total", f"${extracted.get('total_amount', 0.0):,.2f}")

        if extracted.get("line_items"):
          st.dataframe(pd.DataFrame(extracted["line_items"]), hide_index=True)

        if audit.get("status") == "APPROVED":
          st.success(f"✅ **{audit.get('verdict')}**: {audit.get('details')}")
        else:
          st.error(f"🚨 **{audit.get('verdict')}**: {audit.get('details')}")

          st.markdown("---")
          st.markdown("##### 🛡️ Auditor Actions & Agentic Resolution")
          btn_col1, btn_col2 = st.columns(2)

          with btn_col1:
            if st.button("✍️ Force Approve Override", key=f"ovr_{idx}"):
              resp = requests.post(
                  f"{API_BASE_URL}/api/v1/ledger/override",
                  json={
                      "invoice_number": extracted.get("invoice_number"),
                      "vendor_name": extracted.get("vendor_name"),
                      "approved_amount": extracted.get("total_amount", 0.0),
                      "override_reason": "Auditor Exception Approval",
                  },
              )
              if resp.status_code == 200:
                st.success("Override recorded in Invoice_History! Reloading...")
                time.sleep(1.0)
                st.rerun()

          with btn_col2:
            if st.button("🤖 Agent: Draft Vendor Dispute", key=f"dispute_{idx}"):
              disp_resp = requests.post(
                  f"{API_BASE_URL}/api/v1/agent/dispute-email",
                  json={
                      "vendor_name": extracted.get("vendor_name"),
                      "invoice_number": extracted.get("invoice_number"),
                      "po_number": str(audit.get("linked_po", "None")),
                      "billed_amount": extracted.get("total_amount", 0.0),
                      "authorized_amount": float(audit.get("authorized_amount", 0.0)),
                      "discrepancy_details": audit.get("details", ""),
                  },
              )
              if disp_resp.status_code == 200:
                data = disp_resp.json()
                st.session_state[f"email_{idx}"] = data

          if f"email_{idx}" in st.session_state:
            email_info = st.session_state[f"email_{idx}"]
            st.info("✉️ **Automated Supplier Dispute Communication**")
            st.text_input("Subject", value=email_info["subject"], key=f"subj_{idx}")
            st.text_area("Body", value=email_info["body"], height=180, key=f"body_{idx}")
            