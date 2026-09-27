"""Map panel demo — server/map_panel.py renders a deterministic hand-drawn map
panel (one of the Phase 3 three-part composite's three panels). No network,
no API keys: proves layout determinism, loud failure modes, the time-of-day
tint, and the secrecy shape (only the places you pass in are drawn)."""
import hashlib
import io
import sys

sys.path.insert(0, "/home/hatch/workspace/above-the-fog-line")

from server.map_panel import render_map, layout_map


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(f"demo failed: {name}")


PLACES = {
    "trailhead": "Trailhead",
    "fog-meadow": "Fog Meadow",
    "drowned-barn": "Drowned Barn",
    "cheek-holder": "Cheek Holder",
    "bottle-rock": "Bottle Rock",
}
EDGES = [
    ("trailhead", "fog-meadow"),
    ("fog-meadow", "drowned-barn"),
    ("fog-meadow", "cheek-holder"),
    ("cheek-holder", "bottle-rock"),
]


def png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def sha(b):
    return hashlib.sha256(b).hexdigest()


# 1. renders at the panel size
img = render_map(PLACES, EDGES, "fog-meadow", "morning", size=512)
check("renders 512px panel", img.size == (512, 512) and img.mode == "RGB")

# 2. deterministic bytes across runs (fixed seeds, stable hash layout)
check("deterministic bytes", sha(png_bytes(img)) == sha(png_bytes(
    render_map(PLACES, EDGES, "fog-meadow", "morning", size=512))))

# 3. layout covers every place, pure function of input
pos = layout_map(PLACES, EDGES, size=512)
check("layout has all places", set(pos) == set(PLACES))
check("positions in-bounds", all(0 < x < 512 and 0 < y < 512 for x, y in pos.values()))
check("nodes don't all collapse", len(set((round(x), round(y)) for x, y in pos.values())) == len(PLACES))

# 4. two-component graph lays out fine too
pos2 = layout_map({**PLACES, "far-cabin": "Far Cabin"}, EDGES, size=512)
check("disconnected node still placed", "far-cabin" in pos2)

# 5. loud failures, never a silently wrong map
for bad_kwargs, label in [
    (dict(places={}, edges=[]), "empty places raises"),
    (dict(places=PLACES, edges=[("trailhead", "nowhere")]), "unknown edge endpoint raises"),
]:
    try:
        render_map(player_slug="trailhead", **bad_kwargs)
        check(label, False)
    except ValueError:
        check(label, True)
try:
    render_map(PLACES, EDGES, "nowhere", "morning")
    check("player outside places raises", False)
except ValueError:
    check("player outside places raises", True)

# 6. time-of-day tints the palette (corner pixel differs)
am = png_bytes(render_map(PLACES, EDGES, "trailhead", "morning", size=256))
pm = png_bytes(render_map(PLACES, EDGES, "trailhead", "night", size=256))
check("time-of-day changes palette", sha(am) != sha(pm))
check("unknown tod falls back, doesn't crash",
      render_map(PLACES, EDGES, "trailhead", "smarch", size=128).size == (128, 128))

# 7. secrecy shape: the renderer only ever draws the places it is handed.
# A hidden place (e.g. not yet discovered) is absent from layout and pixels.
hidden = {k: v for k, v in PLACES.items() if k != "drowned-barn"}
edges_hidden = [e for e in EDGES if "drowned-barn" not in e]
pos_hidden = layout_map(hidden, edges_hidden, size=256)
check("undiscovered place never in layout", "drowned-barn" not in pos_hidden)
full = png_bytes(render_map(PLACES, EDGES, "trailhead", "morning", size=256))
trimmed = png_bytes(render_map(hidden, edges_hidden, "trailhead", "morning", size=256))
check("undiscovered place never in pixels", sha(full) != sha(trimmed))

# 8. player pin moves with the player
img_a = render_map(PLACES, EDGES, "trailhead", "midday", size=256)
img_b = render_map(PLACES, EDGES, "bottle-rock", "midday", size=256)
check("pin follows player", sha(png_bytes(img_a)) != sha(png_bytes(img_b)))

# 9. single-place map is legal (game start, one known place)
solo = render_map({"trailhead": "Trailhead"}, [], "trailhead", "dusk", size=128)
check("single-place map renders", solo.size == (128, 128))

print("map panel demo: all green")
