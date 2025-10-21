# image_converter_flask.py
from flask import Flask, request, jsonify
from io import BytesIO
from PIL import Image
import requests, base64, os, math, sys, traceback

app = Flask(__name__)

# ===== CONFIG DEFAULTS =====
DEFAULT_RESIZE_FACTOR = 7
DEFAULT_MAX_PIXELS = 40_000_000
USER_AGENT = "ImageConverterFlask/1.0"
REQUEST_TIMEOUT = 12
# ===========================

def logi(msg): print("[Converter][INFO] " + str(msg))
def logd(msg): print("[Converter][DEBUG] " + str(msg))
def logw(msg): print("[Converter][WARN] " + str(msg))
def loge(msg): print("[Converter][ERROR] " + str(msg))

def download_image_bytes(url, timeout=REQUEST_TIMEOUT):
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.content

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

    logi(f"Request: url={url} resize_factor={resize_factor} max_pixels={max_pixels}")

    # Download
    try:
        img_bytes = download_image_bytes(url)
        logd(f"Downloaded {len(img_bytes)} bytes from {url}")
    except Exception as e:
        loge("Download failed: " + str(e))
        return jsonify({"error": f"Failed to download image: {e}"}), 400

    # Open with PIL
    try:
        img = Image.open(BytesIO(img_bytes))
        logd(f"Opened image - mode={img.mode}, size={img.size}, format={img.format}")
    except Exception as e:
        loge("Image open failed: " + str(e))
        return jsonify({"error": f"Failed to open image: {e}"}), 400

    # convert to RGB
    if img.mode != "RGB":
        img = img.convert("RGB")

    orig_w, orig_h = img.size
    logi(f"Original size: {orig_w}x{orig_h}")

    # primary downscale: integer division by resize_factor
    new_w = max(1, orig_w // resize_factor)
    new_h = max(1, orig_h // resize_factor)
    logi(f"Primary downscale: {orig_w}x{orig_h} -> {new_w}x{new_h} (factor {resize_factor})")
    if (new_w, new_h) != (orig_w, orig_h):
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
