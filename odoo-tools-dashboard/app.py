import streamlit as st
import requests
import pandas as pd
from io import BytesIO
import pdfplumber

st.set_page_config(page_title="PDF → Odoo → Excel", page_icon="📦", layout="wide")

# ------------------ ODOO HELPERS ------------------ #
def odoo_auth(url, db, user, password):
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "id": 1,
        "params": {"db": db, "login": user, "password": password},
    }
    r = requests.post(f"{url}/web/session/authenticate",
                      json=payload,
                      headers={"Content-Type": "application/json"},
                      timeout=60)
    res = r.json()
    if not res.get("result") or not res["result"].get("uid"):
        raise Exception("Login failed – credentials check karo")
    return res["result"]["uid"]


def odoo_call(url, model, method, args=None, kwargs=None):
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "id": 2,
        "params": {
            "model": model,
            "method": method,
            "args": args or [],
            "kwargs": kwargs or {},
        },
    }
    r = requests.post(f"{url}/web/dataset/call_kw",
                      json=payload,
                      headers={"Content-Type": "application/json"},
                      timeout=60)
    res = r.json()
    if "error" in res:
        msg = res["error"].get("data", {}).get("message", str(res["error"]))
        raise Exception(msg)
    return res["result"]

# ------------------ PDF → CODES ------------------ #
def extract_codes_from_pdf(file):
    text_all = ""
    with pdfplumber.open(file) as pdf:
        for page in pdf.pages:
            text_all += " " + (page.extract_text() or "")
    # same pattern: [ABC123-1] etc.
    import re
    matches = list({
        m.group(1)
        for m in re.finditer(r"\[([A-Z][A-Z0-9]+(?:-[A-Z0-9]+)*)\]", text_all)
    })
    return matches

# ------------------ STREAMLIT UI ------------------ #
st.title("📦 PDF → Odoo Search → Excel")
st.caption("PDF upload → model codes extract → Odoo search → Excel download")

with st.expander("① Odoo Login Details", expanded=True):
    col1, col2 = st.columns(2)
    with col1:
        odoo_url = st.text_input("Odoo URL", "https://db.swag.com.sa/odoo")
    with col2:
        odoo_db = st.text_input("Database", "db2")

    col3, col4 = st.columns(2)
    with col3:
        odoo_user = st.text_input("Email")
    with col4:
        odoo_pass = st.text_input("Password", type="password")

st.markdown("### ② PDF Upload → Auto Extract Codes")
pdf_file = st.file_uploader("Swag invoice PDF upload karo", type=["pdf"])

codes_text = st.text_area(
    "Extracted Codes (editable, comma separated)",
    height=80,
    placeholder="PDF se codes yahan aayenge, ya manually daal sakte ho",
)

if pdf_file is not None and st.button("📄 PDF se codes nikalo"):
    try:
        codes = extract_codes_from_pdf(pdf_file)
        if not codes:
            st.error("❌ PDF me koi codes nahi mile")
        else:
            st.success(f"✅ {len(codes)} codes mil gaye")
            st.session_state["codes"] = codes
            st.session_state["codes_text"] = ", ".join(codes)
    except Exception as e:
        st.error(f"PDF error: {e}")

if "codes_text" in st.session_state and not codes_text:
    codes_text = st.session_state["codes_text"]

if codes_text:
    st.info("Codes edit kar sakte ho, comma separated.")

result_df = None

if st.button("🔍 Search in Odoo + Excel banao"):
    if not (odoo_url and odoo_db and odoo_user and odoo_pass):
        st.error("Odoo login details complete karo")
    elif not codes_text.strip():
        st.error("Pehle PDF se ya manually codes daalo")
    else:
        codes = [c.strip() for c in codes_text.split(",") if c.strip()]
        try:
            with st.spinner(f"Odoo se connect ho raha hai ({len(codes)} codes)..."):
                uid = odoo_auth(odoo_url.rstrip("/"), odoo_db, odoo_user, odoo_pass)

                # products
                prod_result = odoo_call(
                    odoo_url.rstrip("/"),
                    "product.product",
                    "search_read",
                    args=[[['default_code', 'in', codes]]],
                    kwargs={
                        "fields": [
                            "default_code",
                            "name",
                            "list_price",
                            "compare_list_price",
                            "barcode",
                            "categ_id",
                            "product_tmpl_id",
                        ],
                        "limit": 500,
                    },
                )

                found_codes = [p["default_code"] for p in prod_result]

                # brand map via template
                tmpl_ids = list({p["product_tmpl_id"][0] for p in prod_result})
                brand_map = {}
                if tmpl_ids:
                    tmpl_res = odoo_call(
                        odoo_url.rstrip("/"),
                        "product.template",
                        "search_read",
                        args=[[['id', 'in', tmpl_ids]]],
                        kwargs={
                            "fields": ["id", "product_brand_id"],
                            "limit": 500,
                        },
                    )
                    for t in tmpl_res:
                        brand_map[t["id"]] = (
                            t["product_brand_id"][1] if t.get("product_brand_id") else ""
                        )

                rows = []
                for p in prod_result:
                    rows.append({
                        "Status": "Found",
                        "Internal Ref": p.get("default_code") or "",
                        "Name": p.get("name") or "",
                        "Sales Price": p.get("list_price") or 0,
                        "Compare Price": p.get("compare_list_price") or 0,
                        "Barcode": p.get("barcode") or "",
                        "Brand": brand_map.get(p["product_tmpl_id"][0], ""),
                        "Category": p["categ_id"][1] if p.get("categ_id") else "",
                    })

                missing = [c for c in codes if c not in found_codes]
                for c in missing:
                    rows.append({
                        "Status": "MISSING",
                        "Internal Ref": c,
                        "Name": "NOT FOUND IN ODOO",
                        "Sales Price": "",
                        "Compare Price": "",
                        "Barcode": "",
                        "Brand": "",
                        "Category": "",
                    })

                result_df = pd.DataFrame(rows)
                result_df.sort_values(
                    by=["Status", "Internal Ref"],
                    ascending=[True, True],
                    inplace=True,
                )

            st.success(
                f"Done! Found: {len(prod_result)}, Missing: {len(missing)}, Total: {len(codes)}"
            )
            st.dataframe(result_df, use_container_width=True, height=400)

        except Exception as e:
            st.error(f"❌ Error: {e}\n\nTip: same browser me pehle Odoo login rehna chahiye.")

if "result_df" not in st.session_state and result_df is not None:
    st.session_state["result_df"] = result_df

final_df = result_df or st.session_state.get("result_df")

if final_df is not None:
    def make_excel(df):
        buf = BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Products", index=False)
        buf.seek(0)
        return buf

    st.download_button(
        "⬇ Download Excel",
        data=make_excel(final_df),
        file_name="odoo_products.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
