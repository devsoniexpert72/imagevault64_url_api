# image_converter_flask.py
from flask import Flask, request, jsonify
from io import BytesIO
from PIL import Image, ImageFile
import requests, base64, os, math, sys, traceback, time
from urllib3.util import Retry
from requests.adapters import HTTPAdapter

app = Flask(__name__)

# ===== CONFIG DEFAULTS =====
DEFAULT_RESIZE_FACTOR = 7
DEFAULT_MAX_PIXELS = 40_000_000
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 ImageConverter/1.0"
REQUEST_CONNECT_TIMEOUT = 4.0   # seconds to connect
REQUEST_READ_TIMEOUT = 10.0     # seconds to read
REQUEST_TIMEOUT = (REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT)
MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024  # 80 MB hard cap on raw download
STREAM_CHUNK = 16 * 1024  # 16KB
POOL_MAXSIZE = 50
RETRIES_TOTAL = 3
# ===========================

# Improve PIL resilience for truncated/progressive images
ImageFile.LOAD_TRUNCATED_IMAGES = True
ImageFile.MAX_IMAGE_PIXELS = None  # we'll enforce pixels manually in code

def logi(msg): print("[Converter][INFO] " + str(msg))
def logd(msg): print("[Converter][DEBUG] " + str(msg))
def logw(msg): print("[Converter][WARN] " + str(msg))
def loge(msg): print("[Converter][ERROR] " + str(msg))

# Create a single session with retry + connection pooling and sensible headers
session = requests.Session()
retry_strategy = Retry(
    total=RETRIES_TOTAL,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "OPTIONS"],
    backoff_factor=0.6
)
adapter = HTTPAdapter(max_retries=retry_strategy, pool_maxsize=POOL_MAXSIZE)
session.mount("http://", adapter)
session.mount("https://", adapter)
# Default headers that mimic a real browser and accept modern image formats
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Referer": ""  # intentionally blank; set per-request if needed
}

def is_image_content_type(ct):
    if not ct:
        return False
    ct = ct.lower()
    return ct.startswith("image/")

def safe_head(url, headers, timeout):
    """Attempt HEAD to get content-length and content-type. Some servers block HEAD; return None on failure."""
    try:
        r = session.head(url, headers=headers, allow_redirects=True, timeout=timeout)
        return r
    except Exception as e:
        logd(f"HEAD failed for {url}: {e}")
        return None

def try_alternate_scheme(url):
    """If http -> https or https -> http might help for misconfigured endpoints."""
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    if url.startswith("https://"):
        return "http://" + url[len("https://"):]
    return url

def download_image_bytes(url, timeout=REQUEST_TIMEOUT, max_bytes=MAX_DOWNLOAD_BYTES):
    """
    Robust downloader:
      - Attempts HEAD to check content-type/length
      - Streams GET with chunked reads
      - Enforces max_bytes cutoff
      - Retries automatically via the session adapter
      - Falls back to alternate scheme and a few header variants
    Returns bytes or raises an exception with a helpful message.
    """
    start_ts = time.time()
    headers = dict(BASE_HEADERS)  # copy

    tried_urls = []
    tried_exceptions = []

    candidate_urls = [url]
    # try swapping scheme as a fallback
    alt = try_alternate_scheme(url)
    if alt != url:
        candidate_urls.append(alt)

    # If URL has query params, try stripping them as a last resort (some CDNs choke)
    if "?" in url:
        candidate_urls.append(url.split("?", 1)[0])

    for candidate in candidate_urls:
        tried_urls.append(candidate)
        headers["Referer"] = candidate  # some servers require referer-ish header
        # HEAD check first
        head = safe_head(candidate, headers, timeout)
        content_length = None
        content_type = None
        if head is not None:
            content_length = head.headers.get("Content-Length")
            content_type = head.headers.get("Content-Type")
            logd(f"HEAD: url={candidate} content-type={content_type} content-length={content_length}")
            if content_length:
                try:
                    cl = int(content_length)
                    if cl > max_bytes:
                        raise ValueError(f"Remote content-length {cl} exceeds max allowed {max_bytes} bytes")
                except ValueError as ve:
                    # If content-length is bogus or too large, skip this candidate
                    logw(f"HEAD size check failed for {candidate}: {ve}")
                    tried_exceptions.append(str(ve))
                    continue

            if content_type and not is_image_content_type(content_type):
                logw(f"HEAD says content-type {content_type} is not image; skipping {candidate}")
                tried_exceptions.append(f"non-image content-type {content_type}")
                continue

        # Try GET with stream
        try:
            with session.get(candidate, headers=headers, stream=True, timeout=timeout, allow_redirects=True) as resp:
                resp.raise_for_status()
                # Respect server-provided content-type if present
                ct = resp.headers.get("Content-Type", content_type)
                if ct and not is_image_content_type(ct):
                    raise ValueError(f"Server returned non-image Content-Type: {ct}")

                buf = BytesIO()
                total = 0
                for chunk in resp.iter_content(chunk_size=STREAM_CHUNK):
                    if chunk:
                        buf.write(chunk)
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError(f"Download exceeded max bytes ({max_bytes}) - aborting")
                data = buf.getvalue()
                elapsed = time.time() - start_ts
                logd(f"Downloaded {len(data)} bytes from {candidate} in {elapsed:.2f}s")
                return data
        except Exception as e:
            logw(f"GET failed for {candidate}: {e}")
            tried_exceptions.append(f"{candidate}: {e}")
            # try next candidate
            continue

    # If we reach here, all attempts failed
    raise RuntimeError(f"All download attempts failed for {url}. Tried: {tried_urls}. Errors: {tried_exceptions}")

def pil_to_rgb_bytes(img):
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img.tobytes(), img.width, img.height

@app.route("/render", methods=["POST"])
def render():
    try:
        data = request.get_json(force=True)
    except Exception as e:
        loge("Bad JSON body: " + str(e))
        return jsonify({"error": "Bad JSON body"}), 400

    if not data or "url" not in data:
        return jsonify({"error": "Missing 'url' field"}), 400

    url = data["url"]
    resize_factor = data.get("resize_factor", DEFAULT_RESIZE_FACTOR)
    max_pixels = data.get("max_pixels", DEFAULT_MAX_PIXELS)

    # sanitize resize_factor
    try:
        resize_factor = int(resize_factor)
        if resize_factor < 1:
            resize_factor = DEFAULT_RESIZE_FACTOR
    except Exception:
        resize_factor = DEFAULT_RESIZE_FACTOR

    # sanitize max_pixels
    try:
        max_pixels = int(max_pixels)
        if max_pixels < 1:
            max_pixels = DEFAULT_MAX_PIXELS
    except Exception:
        max_pixels = DEFAULT_MAX_PIXELS

    logi(f"Request: url={url} resize_factor={resize_factor} max_pixels={max_pixels}")

    # Download with robust downloader
    try:
        img_bytes = download_image_bytes(url, timeout=REQUEST_TIMEOUT, max_bytes=MAX_DOWNLOAD_BYTES)
        logd(f"Downloaded {len(img_bytes)} bytes from {url}")
    except Exception as e:
        loge("Download failed: " + str(e))
        return jsonify({"error": f"Failed to download image: {e}"}), 400

    # Open with PIL (use incremental parser for robustness + to accept progressive JPEG/PNG)
    try:
        parser = ImageFile.Parser()
        parser.feed(img_bytes)
        img = parser.close()
        logd(f"Opened image - mode={img.mode}, size={img.size}, format={getattr(img, 'format', None)}")
    except Exception as e:
        # fallback: try Image.open normally
        try:
            img = Image.open(BytesIO(img_bytes))
            logd(f"Fallback open succeeded - mode={img.mode}, size={img.size}, format={img.format}")
        except Exception as e2:
            loge("Image open failed: " + str(e2))
            return jsonify({"error": f"Failed to open image: {e2}"}), 400

    # convert to RGB
    if img.mode != "RGB":
        img = img.convert("RGB")

    orig_w, orig_h = img.size
    logi(f"Original size: {orig_w}x{orig_h}")

    # Primary downscale: use thumbnail (fast, in-place, preserves aspect ratio)
    try:
        # Compute target size after integer division resize_factor
        target_w = max(1, orig_w // resize_factor)
        target_h = max(1, orig_h // resize_factor)
        logi(f"Primary downscale target: {target_w}x{target_h} (factor {resize_factor})")
        # Use LANCZOS via thumbnail by passing a tuple
        img.thumbnail((target_w, target_h), resample=Image.LANCZOS)
        new_w, new_h = img.size
    except Exception as e:
        logw("thumbnail downscale failed, attempting fallback resize: " + str(e))
        new_w = max(1, orig_w // resize_factor)
        new_h = max(1, orig_h // resize_factor)
        img = img.resize((new_w, new_h), Image.LANCZOS)

    # enforce max_pixels: if still larger, scale further proportionally
    total_pixels = new_w * new_h
    if total_pixels > max_pixels:
        scale = math.sqrt(max_pixels / total_pixels)
        final_w = max(1, int(new_w * scale))
        final_h = max(1, int(new_h * scale))
        logw(f"After downscale still too big ({total_pixels} px) -> applying additional scale {scale:.4f} to {final_w}x{final_h}")
        img = img.resize((final_w, final_h), Image.LANCZOS)
        new_w, new_h = final_w, final_h
        total_pixels = new_w * new_h

    logi(f"Final size to encode: {new_w}x{new_h} ({total_pixels} pixels)")

    try:
        raw_bytes = img.tobytes()
    except Exception as e:
        loge("tobytes failed: " + str(e))
        return jsonify({"error": f"Failed to get raw bytes: {e}"}), 500

    # base64 encode
    try:
        b64 = base64.b64encode(raw_bytes).decode("ascii")
    except Exception as e:
        loge("base64 encode failed: " + str(e))
        return jsonify({"error": f"Base64 encode failed: {e}"}), 500

    encoded_mb = (len(b64) * 3 / 4) / (1024*1024)
    logi(f"Encoded payload ~{encoded_mb:.2f} MB (base64 length {len(b64)})")

    # Return trimmed metadata in logs, full b64 in response (Roblox will decode)
    return jsonify({"width": new_w, "height": new_h, "base64_data": b64})

# Auto-launch
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Image -> RawRGB converter (Flask).")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()
    logi(f"Starting Image Converter Flask server on {args.host}:{args.port}")
    try:
        app.run(host=args.host, port=args.port, debug=False)
    except Exception as e:
        loge("Failed to start server: " + str(e))
        traceback.print_exc()
        sys.exit(1)
