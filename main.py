def extract_from_jsonld(soup):
    """חילוץ נתוני מוצר מ-application/ld+json אם קיים (שם יש price/rating)"""
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or tag.text or "{}")
            # לפעמים data הוא list
            if isinstance(data, list):
                for obj in data:
                    if isinstance(obj, dict) and obj.get("@type") == "Product":
                        return obj
            if isinstance(data, dict) and data.get("@type") == "Product":
                return data
        except Exception:
            continue
    return None

def get_product_info(url: str):
    # 1) פתח קישור מקוצר לכתובת הסופית
    final_url = resolve_url(url)
    # הדפסה ללוגים כדי שנדע מה קיבלנו בפועל
    print("[ALI] final_url:", final_url)

    # 2) נסה לחלץ productId מה-URL
    m = re.search(r'/item/(\d{6,20})\.html', final_url)
    product_id = m.group(1) if m else None

    # 3) בקשה לעמוד הדסקטופ הסופי
    r = requests.get(final_url, headers={"User-Agent": USER_AGENT, "Accept-Language":"en-US,en;q=0.9,he;q=0.8"}, timeout=20)
    r.raise_for_status()
    html_text = r.text
    soup = BeautifulSoup(html_text, "html.parser")

    # 4) מקורות אפשריים: JSON-LD, runParams, og:meta
    ld = extract_from_jsonld(soup)
    blob = extract_from_json_blob(html_text)

    # כותרת/תיאור
    title = None
    if ld and isinstance(ld.get("name"), str):
        title = ld["name"]
    if not title and blob:
        title = _deep_find(blob, ["producttitle","subject","title","pagetitle"])
    if not title:
        mt = soup.find("meta", {"property":"og:title"}) or soup.find("meta", {"name":"title"})
        title = (mt.get("content") if mt else None) or (soup.title.string if soup.title else None) or "מוצר מאלי אקספרס"
    title = str(title).strip()

    # תמונה
    image = None
    if ld and ld.get("image"):
        image = ld["image"][0] if isinstance(ld["image"], list) else ld["image"]
    if not image:
        ogimg = soup.find("meta", {"property":"og:image"})
        image = ogimg.get("content") if ogimg else None

    # דירוג
    rating = None
    if ld and isinstance(ld.get("aggregateRating"), dict):
        rating = ld["aggregateRating"].get("ratingValue")
    if not rating and blob:
        rating = _deep_find(blob, ["averagerating","ratingvalue","rating","starrating"])
    rating = str(rating).strip() if rating else None

    # מחיר + מטבע
    price_raw = None
    currency_code = None
    if ld and isinstance(ld.get("offers"), dict):
        price_raw = ld["offers"].get("price") or ld["offers"].get("lowPrice")
        currency_code = ld["offers"].get("priceCurrency")
    if not price_raw and blob:
        price_raw = _deep_find(blob, ["saleprice","price","skuprice","actminprice","displayprice"])
        cur = _deep_find(blob, ["currency","currencysymbol"])
        currency_code = str(cur).upper() if cur else None
    if not currency_code:
        currency_code = detect_currency(str(price_raw) if price_raw else "") or "USD"

    # הזמנות
    orders = None
    if blob:
        orders = _deep_find(blob, ["tradecount","orders","soldcount","ordercount"])

    # 5) אם אין price/rating – נסה דרך עמוד המובייל לפי productId
    if (not price_raw or not rating) and not product_id:
        # נסה לחלץ productId מה-HTML
        m2 = re.search(r'"productId"\s*:\s*"?(\d+)"?', html_text)
        if m2: product_id = m2.group(1)
    print("[ALI] product_id:", product_id)

    if (not price_raw or not rating) and product_id:
        m_url = f"https://m.aliexpress.com/item/{product_id}.html"
        rm = requests.get(m_url, headers={"User-Agent": USER_AGENT, "Accept-Language":"en-US,en;q=0.9,he;q=0.8"}, timeout=20)
        if rm.ok:
            msoup = BeautifulSoup(rm.text, "html.parser")
            script = msoup.find("script", string=re.compile(r'__INIT_DATA__|runParams'))
            data = None
            if script:
                txt = script.string or script.text
                mjson = re.search(r'__INIT_DATA__\s*=\s*(\{.*?\})\s*;', txt, re.DOTALL)
                if mjson:
                    data = json.loads(mjson.group(1))
                else:
                    data = extract_from_json_blob(txt)
            if data:
                if not title:
                    title = str(_deep_find(data, ["producttitle","subject","title"]) or title)
                if not image:
                    image = _deep_find(data, ["image","imageurl","mainimage"])
                if not price_raw:
                    price_raw = _deep_find(data, ["saleprice","discountprice","price","skuprice","minprice","displayprice"])
                if not rating:
                    rating = _deep_find(data, ["averagestar","avgstar","rating","ratingvalue"])
                if not orders:
                    orders = _deep_find(data, ["tradecount","soldcount","orders"])

    # 6) עיבוד והמרה לש״ח
    def fmt_num(n):
        try:
            n = float(n); s = f"{n:.2f}".rstrip("0").rstrip("."); return s
        except: return str(n)

    amount = parse_amount(price_raw) if price_raw else None
    if not currency_code:
        currency_code = "USD"
    price_line = "💰 מחיר: לא זמין"
    if amount:
        if currency_code != "ILS":
            rate = fetch_rate(currency_code, "ILS")
            if rate:
                ils = amount * rate
                price_line = f"💰 מחיר: {fmt_num(ils)} ₪ (≈ {fmt_num(amount)} {currency_code})"
            else:
                price_line = f"💰 מחיר: {fmt_num(amount)} {currency_code}"
        else:
            price_line = f"💰 מחיר: {fmt_num(amount)} ₪"

    rating_line = f"⭐ דירוג: {fmt_num(rating)}" if rating else "⭐ דירוג: לא זמין"
    orders_line = f"📦 {orders} הזמנות" if orders else None

    lines = [f"💥 <b>{html.escape(title)}</b>", rating_line, price_line]
    if orders_line: lines.append(orders_line)
    lines.append("נראה לי דיל ששווה לבדוק לא ? 🤔")
    lines.append(f'🔗 <a href="{html.escape(url)}">לקנייה באלי אקספרס</a>')  # שומר את קישור האפיליאייט שלך
    caption = "\n".join(lines)
    return caption, image
