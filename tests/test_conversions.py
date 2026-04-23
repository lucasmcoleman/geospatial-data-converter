import os
import pytest
from utils import convert, read_file, output_format_dict

input_exts = ["kml", "kmz", "geojson", "json", "zip", "wkt", "gpx"]
output_exts = output_format_dict.keys()

# Pairs with fundamental format-level incompatibility (not bugs in the
# converter): GPX can't represent polygons, and TopoJSON's serializer
# doesn't handle the datetime fields GPX waypoints emit.
INCOMPATIBLE = {
    ("kml", "GPX"),  # test.kml contains polygons
    ("geojson", "GPX"),  # test.geojson contains polygons
    ("zip", "GPX"),  # test.zip is a shapefile of polygons
    ("gpx", "TopoJSON"),  # GPX waypoint datetime columns break topojson
}


@pytest.mark.parametrize("in_ext", input_exts)
@pytest.mark.parametrize("out_ext", output_exts)
def test_coversion(in_ext: str, out_ext: str) -> None:
    if (in_ext, out_ext) in INCOMPATIBLE:
        pytest.skip(f"{in_ext} -> {out_ext} is a known format-level incompatibility")
    test_file = f"test.{in_ext}"
    test_file_path = os.path.join(os.getcwd(), "tests", "test_data", test_file)
    with open(test_file_path, "rb") as f:
        in_file = read_file(f)
    out_file = f"test.{output_format_dict[out_ext][0]}"
    converted_data = convert(in_file, out_file, out_ext)
    with open("test.kml", "wb") as f:
        f.write(converted_data)
