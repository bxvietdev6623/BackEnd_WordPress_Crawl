import requests
from bs4 import BeautifulSoup
from pathlib import Path
import json
import time
import re
from playwright.sync_api import sync_playwright
from flask import Flask, request, jsonify
import os
import sys

# ---------------- CONFIG ----------------
BASE_STORE_URL = "https://www.tadu.com/store/98-a-0-15-a-20-p-{page}-909"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TaduIDBot/1.0)"}
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# Thư mục lưu ảnh cho WordPress theo chuẩn năm/tháng
from datetime import datetime
def get_wp_uploads_dir():
    now = datetime.now()
    year = str(now.year)
    month = f"{now.month:02d}"
    uploads_dir = Path(f"../wp-content/uploads/{year}/{month}")
    uploads_dir.mkdir(parents=True, exist_ok=True)
    return uploads_dir

# ---------------- HELPER REQUESTS ----------------
def safe_get(url, headers=None, timeout=60, retries=3, sleep=2):
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as e:
            print(f"  [Lỗi mạng] {e} (thử {attempt+1}/{retries})")
            time.sleep(sleep)
    raise Exception(f"❌ Không thể truy cập {url} sau {retries} lần!")

def get_book_ids(page: int):
    url = BASE_STORE_URL.format(page=page)
    print(f"\n🔎 Lấy book IDs từ trang: {url}")
    resp = safe_get(url, headers=HEADERS)
    soup = BeautifulSoup(resp.text, "lxml")
    ids = set()
    for a in soup.find_all("a", class_="bookImg", href=True):
        m = re.search(r"/book/(\d+)/", a["href"])
        if m:
            ids.add(m.group(1))
    ids = sorted(ids)
    print(f"✅ Tìm thấy {len(ids)} book IDs trên trang {page}.")
    return ids

def crawl_book_info(book_id: str):
    url = f"https://www.tadu.com/book/{book_id}/"
    print(f"  ➤ Crawl info book: {url}")
    resp = safe_get(url, headers=HEADERS)
    soup = BeautifulSoup(resp.text, "lxml")

    # Title
    title_tag = soup.find("a", class_="bkNm", attrs={"data-name": True})
    title = title_tag["data-name"].strip() if title_tag else ""

    # Author
    author_tag = soup.find("span", class_="author")
    author = author_tag.get_text(strip=True) if author_tag else ""

    # Cover image
    img_tag = soup.find("img", attrs={"data-src": True}) or soup.find("img")
    img_url = ""
    if img_tag:
        img_url = img_tag.get("data-src") or img_tag.get("src") or ""
        if img_url.startswith("//"):
            img_url = "https:" + img_url
        elif img_url.startswith("/"):
            img_url = "https://www.tadu.com" + img_url

    # Fallback nếu ảnh rỗng hoặc media3.tadu.com//
    if not img_url or re.match(r"^https://media\d+\.tadu\.com//?$", img_url):
        meta_img = soup.find("meta", property="og:image")
        if meta_img and meta_img.get("content"):
            img_url = meta_img.get("content")

    # Tải ảnh về thư mục uploads chuẩn WP
    local_img_path = ""
    if img_url:
        try:
            img_resp = requests.get(img_url, headers=HEADERS, timeout=30)
            img_resp.raise_for_status()
            ext = os.path.splitext(img_url)[-1]
            if not ext or len(ext) > 5:
                ext = ".jpg"
            filename = f"{book_id}{ext}"
            uploads_dir = get_wp_uploads_dir()
            local_img_path = str(uploads_dir / filename)
            with open(local_img_path, "wb") as f:
                f.write(img_resp.content)
        except Exception as e:
            print(f"  [Lỗi tải ảnh] {img_url}: {e}")
            local_img_path = ""

    # Description
    intro_tag = soup.find("p", class_="intro")
    description = intro_tag.get_text("\n", strip=True) if intro_tag else ""

    # Genres
    genres = []
    sort_div = soup.find("div", class_="sortList")
    if sort_div:
        genres = [a.get_text(strip=True) for a in sort_div.find_all("a")]

    return {
        "id": book_id,
        "title": title,
        "author": author,
        "cover_image": img_url,
        "cover_image_local": local_img_path.replace("\\", "/"),
        "description": description,
        "genres": genres,
        "url": url
    }

# ---------------- HELPER PLAYWRIGHT ----------------
def crawl_chapter(page, url):
    page.goto(url, timeout=60000)
    page.wait_for_selector("#partContent")

    # Title
    h4_tags = page.query_selector_all("h4")
    if len(h4_tags) >= 2:
        title = h4_tags[1].inner_text().strip()
    elif h4_tags:
        title = h4_tags[0].inner_text().strip()
    else:
        title = "No Title"

    # Content
    content_div = page.query_selector("#partContent")
    paragraphs = []
    if content_div:
        ps = content_div.query_selector_all("p")
        for p in ps:
            text = p.inner_text().strip()
            if text:
                paragraphs.append(text)
    content = "\n".join(paragraphs)

    # Next chapter
    next_chap_tag = page.query_selector("a#paging_right")
    next_url = None
    if next_chap_tag:
        href = next_chap_tag.get_attribute("href")
        if href:
            next_url = "https://www.tadu.com" + href.strip()

    return title, content, next_url

def crawl_first_n_chapters(playwright, start_url, n=NUM_CHAPTERS):
    chapters = []
    url = start_url
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    for i in range(n):
        print(f"  🔹 Crawl chương {i+1}: {url}")
        title, content, next_url = crawl_chapter(page, url)
        chapters.append({"title": title, "content": content, "url": url})
        if not next_url:
            print("  ⚠️ Không tìm thấy chương tiếp theo.")
            break
        url = next_url
        time.sleep(1)
    browser.close()
    return chapters

# ---------------- MAIN ----------------

# ---------------- FLASK API ----------------

app = Flask(__name__)



# Trang chủ: chỉ trả về hướng dẫn sử dụng API
@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "message": "Tadu Books API. Sử dụng endpoint /crawl?page=1&num_chapters=5 để lấy dữ liệu."
    })

@app.route("/crawl", methods=["GET"])
def crawl_api():
    page_num = request.args.get("page", default=1, type=int)
    num_chapters = request.args.get("num_chapters", default=NUM_CHAPTERS, type=int)
    book_ids = get_book_ids(page_num)
    if not book_ids:
        return jsonify({"error": "Không tìm thấy book nào."}), 404

    results = []
    errors = []
    with sync_playwright() as p:
        for idx, book_id in enumerate(book_ids, 1):
            try:
                info = crawl_book_info(book_id)
                start_url = info["url"] + "1/?isfirstpart=true"
                chapters = crawl_first_n_chapters(p, start_url, n=num_chapters)
                info["chapters"] = chapters
                results.append(info)
            except Exception as e:
                errors.append({"id": book_id, "error": str(e)})

    return jsonify({"results": results, "errors": errors})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
