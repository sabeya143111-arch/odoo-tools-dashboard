import streamlit as st
import requests
import pandas as pd
import pdfplumber
from io import BytesIO
import re

# ----------------- STREAMLIT CONFIG ----------------- #
st.set_page_config(
    page_title="Odoo A → Odoo B Product Sync",
    page_icon="🔄",
    layout="wide",
)

st.title("🔄 Odoo A → Odoo B Product Sync")
st.caption(
    "PDF se model codes → Source Odoo A se data → Target Odoo B me auto create / verify."
)

# ----------------- ODOO HELPERS (JSON-RPC) ----------------- #
def call_jsonrpc(url: str, payload: dict) -> dict:
    url = url.rstrip("/")
    r = requests.post(f"{url}/jsonrpc", json=payload, timeout=60)
    try:
        res = r.json()
    except Exception:
        raise Exception(
            f"Odoo response not JSON. Status={r.status_code}, "
            f"Body start: {r.text[:200]!r}"
        )
    if "error" in res:
        msg = res["error"].get("data", {}).get("message", str(res["error"]))
        raise Exception(msg)
    return res["result"]


def odoo_login_full(url, db, user, key):
    """
    Returns (uid, rpc) where rpc(endpoint, method, *args) calls /jsonrpc.
    """
    def rpc(endpoint, method, *args):
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": endpoint, "method": method, "args": list(args)},
        }
        return call_jsonrpc(url, payload)

    uid = rpc("common", "authenticate", db, user, key, {})
    if not uid:
        raise Exception("Login failed (uid=0) – DB / user / key check karo")
    return uid, rpc


def odoo_search_read(rpc, db, uid, key, model, domain, fields, limit=1000):
    return rpc(
        "object",
        "execute_kw",
        db,
        uid,
        key,
        model,
        "search_read",
        [domain],
        {"fields": fields, "limit": limit},
    )


def odoo_create(rpc, db, uid, key, model, vals):
    return rpc(
        "object",
        "execute_kw",
        db,
        uid,
        key,
        model,
        "create",
        [vals],
    )


# ----------------- PDF → CODES ----------------- #
def extract_codes_from_pdf(file) -> list[str]:
    """
    Invoice style: [ABC123-1] etc.
    Regex ko zaroorat ho to change kar sakte ho.
    """
    text = ""
    with pdfplumber.open(file) as pdf:
        for page in pdf.pages:
            text += " " + (page.extract_text() or "")

    pattern = r"\[([A-Z][A-Z0-9]+(?:-[A-Z0-9]+)*)\]"
    codes = list({m.group(1) for m in re.finditer(pattern, text)})
    return sorted(codes)


# ----------------- SIDEBAR: ODOO + OPTIONS ----------------- #
with st.sidebar:
    st.markdown("### 🔐 Odoo Connections")

    st.markdown("**Source Odoo (A)** – data yahan se read hoga")
    url_a = st.text_input("A URL", "https://odooprosys-la-rouche.odoo.com")
    db_a = st.text_input("A DB", "odooprosys-la-rouche-production-12364313")
    user_a = st.text_input("A Email")
    key_a = st.text_input("A API Key / Password", type="password")

    st.markdown("---")
    st.markdown("**Target Odoo (B)** – yahan create/update hoga")
    url_b = st.text_input("B URL", "https://db.swag.com.sa/odoo")
    db_b = st.text_input("B DB", "db2")
    user_b = st.text_input("B Email")
    key_b = st.text_input("B API Key / Password", type="password")

    st.markdown("---")
    price_source = st.selectbox(
        "Source price field (A) → Target list_price (B)",
        options=["compare_list_price", "x_swag_price"],
        index=0,
    )

    mode = st.radio(
        "Mode",
        ["🔎 Preview only", "🧪 Single test create", "⚙️ Full auto (PDF → bulk)"],
    )

# ----------------- STATE INIT ----------------- #
if "codes_list" not in st.session_state:
    st.session_state["codes_list"] = []
if "codes_text" not in st.session_state:
    st.session_state["codes_text"] = ""
if "result_df" not in st.session_state:
    st.session_state["result_df"] = None

# ----------------- PDF + CODES UI ----------------- #
col_pdf, col_manual = st.columns([2, 1])

with col_pdf:
    st.markdown("### ① PDF Upload (optional, bulk)")
    pdf_file = st.file_uploader(
        "Invoice PDF upload karo (codes [ABC123-1] format me)",
        type=["pdf"],
    )
    if pdf_file is not None and st.button("📄 PDF se codes nikalo"):
        try:
            codes = extract_codes_from_pdf(pdf_file)
            if not codes:
                st.error("❌ PDF me koi codes nahi mile. Regex/format check karo.")
            else:
                st.success(f"✅ {len(codes)} codes mil gaye.")
                st.session_state["codes_list"] = codes
                st.session_state["codes_text"] = ", ".join(codes)
        except Exception as e:
            st.error(f"PDF error: {e}")

with col_manual:
    st.markdown("### ② Manual / Edit Codes")
    codes_text = st.text_area(
        "Codes (comma separated)",
        value=st.session_state.get("codes_text", ""),
        height=120,
        placeholder="ABC123-1, PMPQ1091-1, ...",
    )
    st.caption("PDF se aaye codes yahan edit/add/remove kar sakte ho.")

# ----------------- COMMON HELPERS ----------------- #
def connect_both_odoo():
    if not all([url_a, db_a, user_a, key_a, url_b, db_b, user_b, key_b]):
        raise Exception("Odoo A & B dono ke login details complete karo.")
    uid_a, rpc_a = odoo_login_full(url_a, db_a, user_a, key_a)
    uid_b, rpc_b = odoo_login_full(url_b, db_b, user_b, key_b)
    return (uid_a, rpc_a), (uid_b, rpc_b)


def load_products_for_codes(rpc, db, uid, key, codes, extra_price_fields=None):
    fields_prod = [
        "id",
        "default_code",
        "name",
        "product_tmpl_id",
        "barcode",
    ]
    if extra_price_fields:
        fields_prod += extra_price_fields
    prods = odoo_search_read(
        rpc,
        db,
        uid,
        key,
        "product.product",
        [["default_code", "in", codes]],
        fields_prod,
        limit=2000,
    )
    tmpl_ids = list({p["product_tmpl_id"][0] for p in prods})
    tmpls = []
    if tmpl_ids:
        fields_tmpl = [
            "id",
            "name",
            "categ_id",
            "product_brand_id",
            "list_price",
            "compare_list_price",
            "x_swag_price",
        ]
        tmpls = odoo_search_read(
            rpc,
            db,
            uid,
            key,
            "product.template",
            [["id", "in", tmpl_ids]],
            fields_tmpl,
            limit=2000,
        )
    map_prod = {p["default_code"]: p for p in prods if p.get("default_code")}
    map_tmpl = {t["id"]: t for t in tmpls}
    return map_prod, map_tmpl


def get_source_price(prod, tmpl, price_field: str):
    if price_field == "x_swag_price":
        return (tmpl and tmpl.get("x_swag_price")) or prod.get("x_swag_price") or 0.0
    return (tmpl and tmpl.get("compare_list_price")) or prod.get(
        "compare_list_price"
    ) or 0.0


# ----------------- MODE 1: PREVIEW ONLY ----------------- #
if mode == "🔎 Preview only":
    if st.button("🔍 Preview A vs B (no write)"):
        codes = [c.strip() for c in codes_text.split(",") if c.strip()]
        if not codes:
            st.error("Pehle PDF se ya manually model codes daalo.")
        else:
            try:
                with st.spinner("Odoo A & B se connect ho raha hai..."):
                    (uid_a, rpc_a), (uid_b, rpc_b) = connect_both_odoo()

                st.success("✅ Dono Odoo se connection OK. Comparing...")

                # Target B
                map_b, map_tmpl_b = load_products_for_codes(
                    rpc_b, db_b, uid_b, key_b, codes, extra_price_fields=[]
                )

                # Source A (sirf jo B me missing)
                codes_missing_b = [c for c in codes if c not in map_b]
                map_a, map_tmpl_a = (
                    ({}, {})
                    if not codes_missing_b
                    else load_products_for_codes(
                        rpc_a,
                        db_a,
                        uid_a,
                        key_a,
                        codes_missing_b,
                        extra_price_fields=[
                            "list_price",
                            "compare_list_price",
                            "x_swag_price",
                        ],
                    )
                )

                rows = []
                for code in codes:
                    pb = map_b.get(code)
                    pa = map_a.get(code)
                    row = {"Code": code}

                    if pb:
                        row["Status"] = "Already in Target (B)"
                    elif pa:
                        row["Status"] = "Can Create from Source (A)"
                    else:
                        row["Status"] = "Missing in both"

                    # B info
                    if pb:
                        tb = map_tmpl_b.get(pb["product_tmpl_id"][0], {})
                        row["B_Product"] = pb.get("name", "")
                        row["B_Template"] = tb.get("name", "")
                        row["B_Category"] = (
                            tb.get("categ_id", ["", ""])[1] if tb.get("categ_id") else ""
                        )
                        row["B_Brand"] = (
                            tb.get("product_brand_id", ["", ""])[1]
                            if tb.get("product_brand_id")
                            else ""
                        )
                    else:
                        row.update(
                            {
                                "B_Product": "",
                                "B_Template": "",
                                "B_Category": "",
                                "B_Brand": "",
                            }
                        )

                    # A info
                    if pa:
                        ta = map_tmpl_a.get(pa["product_tmpl_id"][0], {})
                        row["A_Product"] = pa.get("name", "")
                        row["A_Template"] = ta.get("name", "")
                        row["A_Category"] = (
                            ta.get("categ_id", ["", ""])[1]
                            if ta.get("categ_id")
                            else (pa.get("categ_id", ["", ""])[1] if pa.get("categ_id") else "")
                        )
                        row["A_Brand"] = (
                            ta.get("product_brand_id", ["", ""])[1]
                            if ta.get("product_brand_id")
                            else ""
                        )
                        row["A_PriceUsed"] = get_source_price(pa, ta, price_source)
                    else:
                        row.update(
                            {
                                "A_Product": "",
                                "A_Template": "",
                                "A_Category": "",
                                "A_Brand": "",
                                "A_PriceUsed": "",
                            }
                        )

                    rows.append(row)

                df = pd.DataFrame(rows)
                st.session_state["result_df"] = df

                st.success("Preview ready.")
                st.dataframe(df, use_container_width=True, height=450)

            except Exception as e:
                st.error(f"❌ Error: {e}")


# ----------------- MODE 2: SINGLE TEST CREATE ----------------- #
if mode == "🧪 Single test create":
    st.markdown("### 🧪 Single product test create")
    test_code = st.text_input("Test model code (default_code)")
    if st.button("Run single test create"):
        code = (test_code or "").strip()
        if not code:
            st.error("Code daalo pehle.")
        else:
            try:
                with st.spinner(f"Connecting & testing code: {code}"):
                    (uid_a, rpc_a), (uid_b, rpc_b) = connect_both_odoo()

                    # Check in B
                    map_b, map_tmpl_b = load_products_for_codes(
                        rpc_b, db_b, uid_b, key_b, [code], extra_price_fields=[]
                    )
                    if code in map_b:
                        st.warning(
                            "Target Odoo B me ye code already exist karta hai. "
                            "Kuch create nahi kiya."
                        )
                        st.write(map_b[code])
                    else:
                        # Get from A
                        map_a, map_tmpl_a = load_products_for_codes(
                            rpc_a,
                            db_a,
                            uid_a,
                            key_a,
                            [code],
                            extra_price_fields=[
                                "list_price",
                                "compare_list_price",
                                "x_swag_price",
                            ],
                        )
                        pa = map_a.get(code)
                        if not pa:
                            st.error("Source Odoo A me bhi ye code nahi mila.")
                        else:
                            ta = map_tmpl_a.get(pa["product_tmpl_id"][0], {})
                            price = get_source_price(pa, ta, price_source)

                            # 1) Ensure template in B
                            tmpl_name = ta.get("name", pa.get("name", code))
                            dom_tmpl = [["name", "=", tmpl_name]]
                            tmpl_search = odoo_search_read(
                                rpc_b,
                                db_b,
                                uid_b,
                                key_b,
                                "product.template",
                                dom_tmpl,
                                ["id", "name"],
                                limit=1,
                            )
                            if tmpl_search:
                                tmpl_b_id = tmpl_search[0]["id"]
                                tmpl_action = "USED_EXISTING_TEMPLATE"
                            else:
                                vals_tmpl = {
                                    "name": tmpl_name,
                                    "categ_id": ta.get("categ_id", [False, False])[0],
                                    "product_brand_id": ta.get("product_brand_id", [False, False])[0],
                                    "list_price": price,
                                }
                                tmpl_b_id = odoo_create(
                                    rpc_b, db_b, uid_b, key_b, "product.template", vals_tmpl
                                )
                                tmpl_action = "CREATED_TEMPLATE"

                            # 2) Create variant in B
                            vals_prod = {
                                "product_tmpl_id": tmpl_b_id,
                                "default_code": code,
                                "barcode": pa.get("barcode"),
                                "lst_price": price,
                            }
                            new_prod_id = odoo_create(
                                rpc_b, db_b, uid_b, key_b, "product.product", vals_prod
                            )

                            st.success(
                                f"✅ Test create done. {tmpl_action}, "
                                f"Template ID B: {tmpl_b_id}, Product ID B: {new_prod_id}"
                            )
                            st.json(
                                {
                                    "code": code,
                                    "template_name": tmpl_name,
                                    "price_used": price,
                                    "product_id_b": new_prod_id,
                                }
                            )
            except Exception as e:
                st.error(f"❌ Error: {e}")


# ----------------- MODE 3: FULL AUTO (PDF → BULK) ----------------- #
if mode == "⚙️ Full auto (PDF → bulk)":
    st.markdown("### ⚙️ Full auto create (use carefully)")
    st.warning(
        "Yeh mode directly Target Odoo B me templates/variants create karega. "
        "Production se pehle staging DB pe test zaroor karo."
    )
    if st.button("Run full auto create for all missing codes"):
        codes = [c.strip() for c in codes_text.split(",") if c.strip()]
        if not codes:
            st.error("Pehle PDF se ya manually model codes daalo.")
        else:
            try:
                with st.spinner("Connecting to Odoo A & B..."):
                    (uid_a, rpc_a), (uid_b, rpc_b) = connect_both_odoo()

                # Existing in B
                map_b, map_tmpl_b = load_products_for_codes(
                    rpc_b, db_b, uid_b, key_b, codes, extra_price_fields=[]
                )

                codes_missing_b = [c for c in codes if c not in map_b]
                if not codes_missing_b:
                    st.info(
                        "Saare codes already Target B me hain, kuch create nahi karne ko."
                    )
                else:
                    st.write(f"Missing in B: {len(codes_missing_b)} codes.")

                    # Load from A
                    map_a, map_tmpl_a = load_products_for_codes(
                        rpc_a,
                        db_a,
                        uid_a,
                        key_a,
                        codes_missing_b,
                        extra_price_fields=[
                            "list_price",
                            "compare_list_price",
                            "x_swag_price",
                        ],
                    )

                    summary_rows = []
                    tmpl_name_to_id_b = {
                        t["name"]: t_id for t_id, t in map_tmpl_b.items()
                    }

                    for code in codes_missing_b:
                        pa = map_a.get(code)
                        if not pa:
                            summary_rows.append(
                                {
                                    "Code": code,
                                    "Action": "NOT FOUND IN A",
                                    "Template_B_ID": "",
                                    "Product_B_ID": "",
                                }
                            )
                            continue

                        ta = map_tmpl_a.get(pa["product_tmpl_id"][0], {})
                        price = get_source_price(pa, ta, price_source)
                        tmpl_name = ta.get("name", pa.get("name", code))

                        # Template in B
                        if tmpl_name in tmpl_name_to_id_b:
                            tmpl_b_id = tmpl_name_to_id_b[tmpl_name]
                            tmpl_action = "USED_EXISTING_TEMPLATE"
                        else:
                            vals_tmpl = {
                                "name": tmpl_name,
                                "categ_id": ta.get("categ_id", [False, False])[0],
                                "product_brand_id": ta.get("product_brand_id", [False, False])[0],
                                "list_price": price,
                            }
                            tmpl_b_id = odoo_create(
                                rpc_b, db_b, uid_b, key_b, "product.template", vals_tmpl
                            )
                            tmpl_name_to_id_b[tmpl_name] = tmpl_b_id
                            tmpl_action = "CREATED_TEMPLATE"

                        # Variant create
                        vals_prod = {
                            "product_tmpl_id": tmpl_b_id,
                            "default_code": code,
                            "barcode": pa.get("barcode"),
                            "lst_price": price,
                        }
                        new_prod_id = odoo_create(
                            rpc_b, db_b, uid_b, key_b, "product.product", vals_prod
                        )

                        summary_rows.append(
                            {
                                "Code": code,
                                "Action": f"{tmpl_action}+CREATED_VARIANT",
                                "Template_B_ID": tmpl_b_id,
                                "Product_B_ID": new_prod_id,
                                "PriceUsed": price,
                            }
                        )

                    df_sum = pd.DataFrame(summary_rows)
                    st.session_state["result_df"] = df_sum
                    st.success("✅ Full auto create completed.")
                    st.dataframe(df_sum, use_container_width=True, height=450)

            except Exception as e:
                st.error(f"❌ Error: {e}")


# ----------------- DOWNLOAD EXCEL (ANY MODE) ----------------- #
final_df = st.session_state.get("result_df")
if final_df is not None and not final_df.empty:
    def make_excel(df: pd.DataFrame) -> BytesIO:
        buf = BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Result", index=False)
        buf.seek(0)
        return buf

    st.download_button(
        "⬇ Download result Excel",
        data=make_excel(final_df),
        file_name="odoo_sync_result.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
