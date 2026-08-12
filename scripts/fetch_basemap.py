"""
Fetch real aerial imagery for the site extent (Esri World Imagery export, no
API key needed) so the tool shows actual park context instead of an abstract
grid. Requested directly in the site's own CRS (EPSG:6343, bboxSR/imageSR) at
1 pixel/metre -- no reprojection needed, pixel-for-pixel aligned with the
ground plane's local coordinates.
"""
import io

import requests
from PIL import Image

from common import PATHS, PROJECT_DIR

OUT_PATH = PROJECT_DIR / "web" / "data" / "basemap.jpg"


def main():
    with __import__("rasterio").open(PATHS["DSM"]) as ref:
        b = ref.bounds

    # request pixels-per-metre, not the (much coarser) 2m analysis grid --
    # this is a visual/context layer, want it sharp
    px_per_m = 2
    w, h = round((b.right - b.left) * px_per_m), round((b.top - b.bottom) * px_per_m)

    url = (
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export"
        f"?bbox={b.left},{b.bottom},{b.right},{b.top}&bboxSR=6343&imageSR=6343"
        f"&size={w},{h}&format=png32&f=image"
    )
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    print(f"Fetched {img.size} aerial image, {len(r.content)} bytes as PNG")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT_PATH, "JPEG", quality=88)
    print(f"Wrote {OUT_PATH} ({OUT_PATH.stat().st_size} bytes as JPEG)")


if __name__ == "__main__":
    main()
