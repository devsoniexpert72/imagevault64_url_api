# image_converter_flask.py
from flask import Flask, request, jsonify
from io import BytesIO
from PIL import Image
import requests
import base64

app = Flask(__name__)

# CONFIG
DEFAULT_MAX_PIXELS = 40_000_000  # safety limit
DOWNSCALE_FACTOR = 3            # 5x reduction in each dimension

def download_image_bytes(url: str, timeout: int = 10) -> bytes:
    """Downloads image bytes from a URL."""
    headers = {"User-Agent": "ImageConverterFlask/1.0"}
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.content

@app.route("/render", methods=["POST"])
def render():
    try:
        data = request.get_json(force=True)
        if not data or "url" not in data:
            return jsonify({"error": "Missing 'url' field in request body"}), 400

        img_url = data["url"]
        max_pixels = int(data.get("max_pixels", DEFAULT_MAX_PIXELS))

        # Download
        try:
            img_bytes = download_image_bytes(img_url)
        except Exception as e:
            return jsonify({"error": f"Failed to download image: {e}"}), 400

        # Decode and convert
        try:
            img = Image.open(BytesIO(img_bytes))
        except Exception as e:
            return jsonify({"error": f"Failed to open image: {e}"}), 400

        if img.mode != "RGB":
            img = img.convert("RGB")

        orig_w, orig_h = img.size

        # Downscale by factor of 5
        new_w = max(1, orig_w // DOWNSCALE_FACTOR)
        new_h = max(1, orig_h // DOWNSCALE_FACTOR)

        if (new_w, new_h) != (orig_w, orig_h):
            img = img.resize((new_w, new_h), Image.LANCZOS)

        total_pixels = new_w * new_h

        # Enforce maximum pixel safety
        if total_pixels > max_pixels:
            scale = (max_pixels / total_pixels) ** 0.5
            final_w = max(1, int(new_w * scale))
            final_h = max(1, int(new_h * scale))
            img = img.resize((final_w, final_h), Image.LANCZOS)
            new_w, new_h = final_w, final_h

        # Convert to raw RGB bytes
        raw_bytes = img.tobytes()
        b64 = base64.b64encode(raw_bytes).decode("ascii")

        print(f"[INFO] Processed {img_url} ({orig_w}x{orig_h} → {new_w}x{new_h})")

        return jsonify({
            "width": new_w,
            "height": new_h,
            "base64_data": b64
        })

    except Exception as e:
        return jsonify({"error": f"Unexpected error: {e}"}), 500

# AUTO-LAUNCH
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    print(f"🚀 Image Converter Flask Server running on port {port}")
    print("POST JSON to /render with {'url': 'https://example.com/image.png'}")
    app.run(host="0.0.0.0", port=port, debug=False)
