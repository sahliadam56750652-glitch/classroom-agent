import sqlite3, textwrap
c = sqlite3.connect(r"data\academic.db")
rows = c.execute("""
    SELECT drive_id, page_index, LENGTH(text), text
    FROM ocr_pages WHERE status='ok'
    ORDER BY LENGTH(text) DESC LIMIT 3
""").fetchall()
for d, i, n, t in rows:
    print("=" * 70)
    print(f"drive_id {d}  page {i}  ({n} chars)")
    print("=" * 70)
    print(t[:2000])
    print()
