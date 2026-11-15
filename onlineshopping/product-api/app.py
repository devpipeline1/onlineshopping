import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from pathlib import Path
from tempfile import NamedTemporaryFile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import psycopg
from psycopg.rows import dict_row

PORT = int(os.environ.get("PORT", "5001"))
DATABASE_URL = os.environ["DATABASE_URL"]
SHARED_DIR = Path(os.environ.get("SHARED_DIR", "/shared"))
CATALOG_FILE = SHARED_DIR / "catalog" / "products.json"


def write_catalog_snapshot():
    """Persist a catalogue snapshot to this Product API Pod's EBS-backed volume."""
    try:
        CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with connect() as conn:
            rows = conn.execute("SELECT id, name, description, price, stock, image_url, updated_at FROM products ORDER BY id").fetchall()
        payload = [{**dict(row), "price": float(row["price"]), "updated_at": row["updated_at"].isoformat()} for row in rows]
        temp_path = CATALOG_FILE.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(CATALOG_FILE)
        print(f"Catalogue snapshot written to {CATALOG_FILE}", flush=True)
    except Exception as exc:
        # The database remains the source of truth; report NFS snapshot errors clearly.
        print(f"WARNING: could not write persistent catalogue snapshot: {exc}", flush=True)
SHARED_DIR = Path(os.environ.get("SHARED_DIR", "/shared"))
CATALOG_FILE = SHARED_DIR / "catalog" / "products.json"


def write_catalog_snapshot():
    """Persist a catalogue snapshot to this Product API Pod's EBS-backed volume."""
    try:
        CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with connect() as conn:
            rows = conn.execute("SELECT id, name, description, price, stock, image_url, updated_at FROM products ORDER BY id").fetchall()
        payload = [{**dict(row), "price": float(row["price"]), "updated_at": row["updated_at"].isoformat()} for row in rows]
        temp_path = CATALOG_FILE.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(CATALOG_FILE)
        print(f"Catalogue snapshot written to {CATALOG_FILE}", flush=True)
    except Exception as exc:
        # The database remains the source of truth; report NFS snapshot errors clearly.
        print(f"WARNING: could not write persistent catalogue snapshot: {exc}", flush=True)


def connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def initialize_database():
    # Retry briefly while PostgreSQL starts during local Compose/Kubernetes startup.
    import time
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS products (
                        id BIGSERIAL PRIMARY KEY,
                        name VARCHAR(120) NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        price NUMERIC(12, 2) NOT NULL CHECK (price >= 0),
                        stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
                        image_url TEXT NOT NULL DEFAULT '',
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                conn.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS image_url TEXT NOT NULL DEFAULT ''")
                conn.execute("""
                    INSERT INTO products (name, description, price, stock, image_url)
                    SELECT v.name, v.description, v.price, v.stock, v.image_url
                    FROM (VALUES
                        ('Laptop', 'Everyday laptop for work and study', 65000.00, 12, 'https://images.unsplash.com/photo-1496181133206-80ce9b88a853?w=900&auto=format&fit=crop'),
                        ('Headphones', 'Wireless over-ear headphones', 2500.00, 35, 'https://images.unsplash.com/photo-1505740420928-5e560c06d30e?w=900&auto=format&fit=crop'),
                        ('Mechanical Keyboard', 'Mechanical keyboard for your desk setup', 1800.00, 20, 'https://images.unsplash.com/photo-1587829741301-dc798b83add3?w=900&auto=format&fit=crop'),
                        ('Smart Watch', 'Fitness and notification smartwatch', 4999.00, 18, 'https://images.unsplash.com/photo-1523275335684-37898b6baf30?w=900&auto=format&fit=crop'),
                        ('Camera', 'Compact camera for everyday photography', 32999.00, 7, 'https://images.unsplash.com/photo-1516035069371-29a1b244cc32?w=900&auto=format&fit=crop')
                    ) AS v(name, description, price, stock, image_url)
                    WHERE NOT EXISTS (SELECT 1 FROM products)
                """)
            print("Database initialized", flush=True)
            write_catalog_snapshot()
            return
        except Exception as exc:
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"Could not initialize PostgreSQL: {last_error}")


def product_json(row):
    item = dict(row)
    item["price"] = float(item["price"])
    item["image_url"] = item.get("image_url", "")
    item["updated_at"] = item["updated_at"].isoformat()
    return item


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("Request body is required")
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        path = urlparse(self.path).path
        # ALB route uses /product; retain /products for backwards compatibility.
        if path == "/product" or path.startswith("/product/"):
            path = "/products" + path[len("/product"):]
        if path == "/health":
            try:
                with connect() as conn:
                    conn.execute("SELECT 1")
                return self.send_json(200, {"status": "ok", "service": "product-api", "database": "connected"})
            except Exception as exc:
                return self.send_json(503, {"status": "unavailable", "error": str(exc)})
        if path == "/products":
            with connect() as conn:
                rows = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
                latest = conn.execute("SELECT COALESCE(MAX(EXTRACT(EPOCH FROM updated_at)), 0) AS version FROM products").fetchone()
            return self.send_json(200, {"service": "product-api", "version": float(latest["version"]), "products": [product_json(r) for r in rows]})
        return self.send_json(404, {"error": "not found", "path": path})

    def do_POST(self):
        request_path = urlparse(self.path).path
        if request_path not in ("/products", "/product"):
            return self.send_json(404, {"error": "not found"})
        try:
            data = self.read_json()
            name = str(data.get("name", "")).strip()
            description = str(data.get("description", "")).strip()
            price = float(data.get("price", -1))
            stock = int(data.get("stock", 0))
            if not name or price < 0 or stock < 0:
                raise ValueError("Name is required; price and stock must be zero or greater")
            with connect() as conn:
                row = conn.execute("INSERT INTO products (name, description, price, stock, image_url) VALUES (%s, %s, %s, %s, %s) RETURNING *", (name, description, price, stock, str(data.get("image_url", "")).strip())).fetchone()
            write_catalog_snapshot()
            write_catalog_snapshot()
            return self.send_json(201, {"message": "Product created", "product": product_json(row)})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self.send_json(400, {"error": str(exc)})

    def do_PUT(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) != 2 or parts[0] not in ("products", "product"):
            return self.send_json(404, {"error": "not found"})
        try:
            product_id = int(parts[1])
            data = self.read_json()
            name = str(data.get("name", "")).strip()
            description = str(data.get("description", "")).strip()
            price = float(data.get("price", -1))
            stock = int(data.get("stock", -1))
            if not name or price < 0 or stock < 0:
                raise ValueError("Name is required; price and stock must be zero or greater")
            with connect() as conn:
                row = conn.execute("UPDATE products SET name=%s, description=%s, price=%s, stock=%s, image_url=%s, updated_at=NOW() WHERE id=%s RETURNING *", (name, description, price, stock, str(data.get("image_url", "")).strip(), product_id)).fetchone()
            if not row:
                return self.send_json(404, {"error": "product not found"})
            write_catalog_snapshot()
            write_catalog_snapshot()
            return self.send_json(200, {"message": "Product updated", "product": product_json(row)})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self.send_json(400, {"error": str(exc)})

    def do_DELETE(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) != 2 or parts[0] not in ("products", "product"):
            return self.send_json(404, {"error": "not found"})
        try:
            product_id = int(parts[1])
            with connect() as conn:
                row = conn.execute("DELETE FROM products WHERE id=%s RETURNING id", (product_id,)).fetchone()
            if not row:
                return self.send_json(404, {"error": "product not found"})
            write_catalog_snapshot()
            write_catalog_snapshot()
            return self.send_json(200, {"message": "Product deleted", "id": product_id})
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc)})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


initialize_database()
print(f"product-api listening on 0.0.0.0:{PORT}", flush=True)
ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
