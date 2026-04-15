import io
import json
import os
import zipfile
import geopandas as gpd
import pandas as pd
import topojson

from shapely import wkt as shapely_wkt
from shapely.geometry import (
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)
from tempfile import TemporaryDirectory
from typing import BinaryIO
from kml_tricks import load_ge_data

output_format_dict = {
    "CSV": ("csv", "csv", "text/csv"),
    "KML": ("kml", "kml", "application/vnd.google-earth.kml+xml"),
    "GeoJSON": ("geojson", "geojson", "application/geo+json"),
    "TopoJSON": ("topojson", "topojson", "application/json"),
    "WKT": ("wkt", "wkt", "text/plain"),
    "EsriJSON": ("json", "json", "application/json"),
    "GPX": ("gpx", "gpx", "application/gpx+xml"),
    "ESRI Shapefile": ("shp", "zip", "application/zip"),  # must be zipped
    "OpenFileGDB": ("gdb", "zip", "application/zip"),  # must be zipped
}


_ESRI_GEOMETRY_TYPES = {
    "Point": "esriGeometryPoint",
    "MultiPoint": "esriGeometryMultipoint",
    "LineString": "esriGeometryPolyline",
    "MultiLineString": "esriGeometryPolyline",
    "Polygon": "esriGeometryPolygon",
    "MultiPolygon": "esriGeometryPolygon",
}


def _shapely_to_esri_geometry(geom, sr: dict) -> dict:
    """Convert a shapely geometry to an Esri JSON geometry dict."""
    if geom is None:
        return None
    if isinstance(geom, Point):
        return {"x": geom.x, "y": geom.y, "spatialReference": sr}
    if isinstance(geom, MultiPoint):
        return {
            "points": [[pt.x, pt.y] for pt in geom.geoms],
            "spatialReference": sr,
        }
    if isinstance(geom, LineString):
        return {"paths": [list(map(list, geom.coords))], "spatialReference": sr}
    if isinstance(geom, MultiLineString):
        return {
            "paths": [list(map(list, line.coords)) for line in geom.geoms],
            "spatialReference": sr,
        }
    if isinstance(geom, Polygon):
        rings = [list(map(list, geom.exterior.coords))]
        rings.extend(list(map(list, ring.coords)) for ring in geom.interiors)
        return {"rings": rings, "spatialReference": sr}
    if isinstance(geom, MultiPolygon):
        rings = []
        for poly in geom.geoms:
            rings.append(list(map(list, poly.exterior.coords)))
            rings.extend(list(map(list, ring.coords)) for ring in poly.interiors)
        return {"rings": rings, "spatialReference": sr}
    raise ValueError(f"Unsupported geometry type for EsriJSON: {geom.geom_type}")


def gdf_to_esrijson(gdf: gpd.GeoDataFrame) -> str:
    """Serialize a GeoDataFrame to an Esri Feature JSON (FeatureSet) string."""
    wkid = None
    if gdf.crs is not None:
        try:
            wkid = gdf.crs.to_epsg()
        except Exception:
            wkid = None
    sr = {"wkid": wkid} if wkid else {"wkid": 4326}

    geom_type = None
    for g in gdf.geometry:
        if g is not None:
            geom_type = _ESRI_GEOMETRY_TYPES.get(g.geom_type)
            break

    features = []
    attrs_df = gdf.drop(columns=[gdf.geometry.name])
    for geom, (_, row) in zip(gdf.geometry, attrs_df.iterrows()):
        attributes = {}
        for col, val in row.items():
            if pd.isna(val):
                attributes[col] = None
            elif hasattr(val, "item"):
                attributes[col] = val.item()
            else:
                attributes[col] = val
        features.append(
            {
                "attributes": attributes,
                "geometry": _shapely_to_esri_geometry(geom, sr),
            },
        )

    feature_set = {
        "geometryType": geom_type,
        "spatialReference": sr,
        "features": features,
    }
    return json.dumps(feature_set, default=str)


def read_wkt_text(text: str) -> gpd.GeoDataFrame:
    """Parse WKT text (one geometry per line) into a GeoDataFrame.

    Blank lines and lines starting with '#' are ignored. The resulting
    GeoDataFrame uses EPSG:4326 as its CRS.
    """
    geometries = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        geometries.append(shapely_wkt.loads(stripped))
    return gpd.GeoDataFrame(geometry=geometries, crs="EPSG:4326")


def read_wkt(file: BinaryIO) -> gpd.GeoDataFrame:
    """Read a WKT file and return a GeoDataFrame."""
    content = file.read()
    if isinstance(content, bytes):
        content = content.decode("utf-8")
    return read_wkt_text(content)


def read_gpx(file_path: str) -> gpd.GeoDataFrame:
    """Read a GPX file, returning the first non-empty standard layer.

    GPX files can contain multiple layers (waypoints, routes, tracks,
    track_points, route_points). We try them in a sensible order and
    return whichever has features.
    """
    for layer in ("waypoints", "tracks", "routes", "track_points", "route_points"):
        try:
            gdf = gpd.read_file(file_path, layer=layer, engine="pyogrio")
        except Exception:
            continue
        if len(gdf) > 0:
            return gdf
    # Fall back to driver default
    return gpd.read_file(file_path, engine="pyogrio")


def write_gpx(gdf: gpd.GeoDataFrame, out_path: str) -> None:
    """Write a GeoDataFrame to GPX, choosing a layer based on geometry type."""
    if len(gdf) == 0:
        raise ValueError("Cannot write an empty GeoDataFrame to GPX.")

    # GPX is strictly WGS 84.
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")

    geom_types = set(gdf.geometry.geom_type.dropna().unique())
    if geom_types <= {"Point"}:
        layer = "waypoints"
    elif geom_types <= {"LineString", "MultiLineString"}:
        layer = "tracks"
    else:
        raise ValueError(
            "GPX only supports Point or LineString/MultiLineString geometries; "
            f"got {sorted(geom_types)}.",
        )

    # GPX has a fixed schema (ele, time, name, cmt, desc, sym, type, ...).
    # Dropping fields outside that schema is the most reliable way to
    # produce a valid GPX file across GDAL versions; otherwise the driver
    # errors on unknown fields. Preserve 'name' since it's the primary
    # waypoint/track label users expect to see.
    _GPX_STANDARD_FIELDS = {
        "ele",
        "time",
        "magvar",
        "geoidheight",
        "name",
        "cmt",
        "desc",
        "src",
        "sym",
        "type",
        "fix",
        "sat",
        "hdop",
        "vdop",
        "pdop",
        "ageofdgpsdata",
        "dgpsid",
    }
    geom_col = gdf.geometry.name
    keep_cols = [
        c
        for c in gdf.columns
        if c == geom_col or str(c).lower() in _GPX_STANDARD_FIELDS
    ]
    gdf.loc[:, keep_cols].to_file(
        out_path,
        driver="GPX",
        engine="pyogrio",
        layer=layer,
    )


def _esri_geometry_to_shapely(geom: dict):
    """Convert an Esri JSON geometry dict to a shapely geometry."""
    if geom is None:
        return None
    if "x" in geom and "y" in geom:
        return Point(geom["x"], geom["y"])
    if "points" in geom:
        return MultiPoint(geom["points"])
    if "paths" in geom:
        paths = geom["paths"]
        if len(paths) == 1:
            return LineString(paths[0])
        return MultiLineString(paths)
    if "rings" in geom:
        rings = [[(pt[0], pt[1]) for pt in ring] for ring in geom["rings"]]
        polygons = []
        for ring in rings:
            shell = Polygon(ring)
            if polygons and not shell.exterior.is_ccw:
                # Inner ring of previous polygon
                outer = polygons[-1]
                polygons[-1] = Polygon(outer.exterior.coords, list(outer.interiors) + [ring])
            else:
                polygons.append(shell)
        if len(polygons) == 1:
            return polygons[0]
        return MultiPolygon(polygons)
    raise ValueError(f"Unrecognized Esri JSON geometry: {list(geom)}")


def read_esrijson(feature_set: dict) -> gpd.GeoDataFrame:
    """Convert a parsed Esri Feature JSON dict to a GeoDataFrame."""
    features = feature_set.get("features", [])
    sr = feature_set.get("spatialReference") or {}
    wkid = sr.get("latestWkid") or sr.get("wkid") or 4326
    crs = f"EPSG:{wkid}"

    records = []
    geometries = []
    for feat in features:
        records.append(feat.get("attributes") or {})
        geometries.append(_esri_geometry_to_shapely(feat.get("geometry")))

    return gpd.GeoDataFrame(records, geometry=geometries, crs=crs)


def read_file(file: BinaryIO, *args, **kwargs) -> gpd.GeoDataFrame:
    """Read a file and return a GeoDataFrame"""
    basename, ext = os.path.splitext(os.path.basename(file.name))
    ext = ext.lower().strip(".")
    if ext == "zip":
        with TemporaryDirectory() as tmp_dir:
            tmp_file_path = os.path.join(tmp_dir, f"{basename}.{ext}")
            with open(tmp_file_path, "wb") as tmp_file:
                tmp_file.write(file.read())
            return gpd.read_file(
                f"zip://{tmp_file_path}",
                *args,
                engine="pyogrio",
                **kwargs,
            )
    elif ext in ("kml", "kmz"):
        with TemporaryDirectory() as tmp_dir:
            tmp_file_path = os.path.join(tmp_dir, f"{basename}.{ext}")
            with open(tmp_file_path, "wb") as tmp_file:
                tmp_file.write(file.read())
            return load_ge_data(tmp_file_path)
    elif ext == "wkt":
        return read_wkt(file)
    elif ext == "gpx":
        with TemporaryDirectory() as tmp_dir:
            tmp_file_path = os.path.join(tmp_dir, f"{basename}.{ext}")
            with open(tmp_file_path, "wb") as tmp_file:
                tmp_file.write(file.read())
            return read_gpx(tmp_file_path)
    elif ext in ("json", "geojson"):
        # Handle both GeoJSON and Esri Feature JSON (as produced by ArcGIS REST).
        data = file.read()
        try:
            parsed = json.loads(data)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and (
            "features" in parsed
            and parsed.get("features")
            and isinstance(parsed["features"][0], dict)
            and "attributes" in parsed["features"][0]
        ):
            return read_esrijson(parsed)
        # Fall back to pyogrio for GeoJSON and other JSON-based formats.
        with TemporaryDirectory() as tmp_dir:
            tmp_file_path = os.path.join(tmp_dir, f"{basename}.{ext}")
            with open(tmp_file_path, "wb") as tmp_file:
                tmp_file.write(data)
            return gpd.read_file(
                tmp_file_path,
                *args,
                engine="pyogrio",
                **kwargs,
            )
    return gpd.read_file(file, *args, engine="pyogrio", **kwargs)


def zip_dir(directory: str) -> bytes:
    """Zip a directory and return the bytes"""
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(directory):
            for file in files:
                new_member = os.path.join(root, file)
                zipf.write(
                    new_member,
                    os.path.relpath(new_member, directory),
                )

    return zip_buffer.getvalue()


def convert(gdf: gpd.GeoDataFrame, output_name: str, output_format: str) -> bytes:
    """Convert a GeoDataFrame to the specified format"""
    with TemporaryDirectory() as tmpdir:
        out_path = os.path.join(tmpdir, output_name)
        if output_format == "CSV":
            gdf.to_csv(out_path)
        elif output_format == "TopoJSON":
            topojson_data = topojson.Topology(gdf)
            topojson_data.to_json(out_path)
        elif output_format == "WKT":
            with open(out_path, "w", encoding="utf-8") as wkt_file:
                wkt_file.write(
                    "\n".join(
                        "" if geom is None else geom.wkt for geom in gdf.geometry
                    ),
                )
        elif output_format == "EsriJSON":
            with open(out_path, "w", encoding="utf-8") as esri_file:
                esri_file.write(gdf_to_esrijson(gdf))
        elif output_format == "GPX":
            write_gpx(gdf, out_path)
        else:
            gdf.to_file(out_path, driver=output_format, engine="pyogrio")

        if output_format in ("ESRI Shapefile", "OpenFileGDB"):
            output_bytes = zip_dir(tmpdir)
        else:
            with open(out_path, "rb") as f:
                output_bytes = f.read()

        return output_bytes
