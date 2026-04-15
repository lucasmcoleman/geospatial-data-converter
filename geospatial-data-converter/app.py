import asyncio
import io
import json
import math
import os
import zipfile

import geopandas as gpd
import pydeck as pdk
import streamlit as st
from aiohttp import ClientSession
from restgdf import FeatureLayer

from utils import read_file, read_wkt_text, convert, output_format_dict

__version__ = "1.2.0"
APP_NAME = "Geospatial Data Converter"
MAP_FEATURE_LIMIT = 5000


INPUT_FORMAT_HELP = (
    "Supported uploads: KML, KMZ, GeoJSON (.geojson), "
    "Esri Feature JSON (.json), WKT (.wkt), GPX (.gpx), "
    "ZIP (shapefile or file geodatabase)."
)

OUTPUT_FORMAT_HELP = {
    "CSV": "Comma-separated values; geometry is serialised as WKT.",
    "KML": "Google Earth Keyhole Markup Language.",
    "GeoJSON": "RFC 7946 GeoJSON (text).",
    "TopoJSON": "Topology-preserving JSON.",
    "WKT": "One Well-Known Text geometry per line.",
    "EsriJSON": "Esri Feature JSON (FeatureSet) as consumed by ArcGIS Pro.",
    "GPX": "GPS Exchange Format (waypoints or tracks only).",
    "ESRI Shapefile": "Zipped shapefile (.shp, .shx, .dbf, .prj).",
    "OpenFileGDB": "Zipped File Geodatabase.",
}

# CRS presets shown in the reprojection picker. Values are either an int
# EPSG code, None (no reprojection), or a sentinel string handled below.
CRS_PRESETS = {
    "Keep source CRS": None,
    "EPSG:4326 — WGS 84 (lat/lon)": 4326,
    "EPSG:3857 — Web Mercator": 3857,
    "EPSG:4269 — NAD83": 4269,
    "Auto UTM zone (from bbox)": "auto_utm",
    "Custom EPSG…": "custom",
}


def st_init_null(*variable_names) -> None:
    for variable_name in variable_names:
        if variable_name not in st.session_state:
            st.session_state[variable_name] = None


st_init_null(
    "datasets",
    "load_error",
    "converted_data",
    "converted_fn",
    "converted_mime",
)


# --- Page config ---
st.set_page_config(
    page_title=f"{APP_NAME} v{__version__}",
    page_icon="🌎",
    layout="wide",
)


async def get_arcgis_data(url: str) -> tuple[str, gpd.GeoDataFrame]:
    """Get data from an ArcGIS feature layer"""
    async with ClientSession() as session:
        rest = await FeatureLayer.from_url(url, session=session)
        name = rest.name
        gdf = await rest.getgdf()
    return name, gdf


def _reset_converted() -> None:
    st.session_state.converted_data = None
    st.session_state.converted_fn = None
    st.session_state.converted_mime = None


def _clear_all() -> None:
    st.session_state.datasets = None
    st.session_state.load_error = None
    _reset_converted()


def _utm_epsg_for_gdf(gdf: gpd.GeoDataFrame) -> int:
    """Pick an appropriate UTM zone EPSG code for a GeoDataFrame's centroid."""
    src = gdf
    if src.crs is not None and src.crs.to_epsg() != 4326:
        src = src.to_crs(4326)
    minx, miny, maxx, maxy = src.total_bounds
    lon = (minx + maxx) / 2.0
    lat = (miny + maxy) / 2.0
    zone = int((lon + 180.0) / 6.0) + 1
    zone = max(1, min(60, zone))
    return (32600 if lat >= 0 else 32700) + zone


def _resolve_target_crs(choice: str, custom_epsg: str, gdf: gpd.GeoDataFrame):
    """Return the EPSG int to reproject to, or None to keep source."""
    spec = CRS_PRESETS[choice]
    if spec is None:
        return None
    if spec == "auto_utm":
        return _utm_epsg_for_gdf(gdf)
    if spec == "custom":
        if not custom_epsg:
            return None
        return int(custom_epsg)
    return spec


def _transform_gdf(
    gdf: gpd.GeoDataFrame,
    target_crs=None,
    columns=None,
    fix_invalid: bool = False,
) -> gpd.GeoDataFrame:
    """Apply reprojection / column subset / validity fix in that order."""
    if fix_invalid:
        gdf = gdf.copy()
        try:
            gdf.geometry = gdf.geometry.make_valid()
        except Exception:
            gdf.geometry = gdf.geometry.buffer(0)
    if target_crs is not None:
        gdf = gdf.to_crs(target_crs)
    if columns is not None:
        geom_col = gdf.geometry.name
        keep = [c for c in columns if c in gdf.columns and c != geom_col]
        gdf = gdf[keep + [geom_col]]
    return gdf


def _render_convert_controls(
    gdf: gpd.GeoDataFrame,
    allow_columns: bool,
    key_prefix: str,
) -> dict:
    """Render the shared convert controls; return a dict of choices."""
    output_format = st.selectbox(
        "Output format",
        list(output_format_dict.keys()),
        key=f"{key_prefix}_output_format",
    )
    st.caption(OUTPUT_FORMAT_HELP.get(output_format, ""))

    crs_choice = st.selectbox(
        "Target CRS",
        list(CRS_PRESETS.keys()),
        key=f"{key_prefix}_crs_choice",
        help="Reproject the data before export.",
    )
    custom_epsg = ""
    if CRS_PRESETS[crs_choice] == "custom":
        custom_epsg = st.text_input(
            "Custom EPSG code",
            placeholder="e.g. 26910",
            key=f"{key_prefix}_custom_epsg",
        )

    fix_invalid = st.checkbox(
        "Fix invalid geometries",
        value=False,
        key=f"{key_prefix}_fix_invalid",
        help="Run make_valid() before export. Useful for polygons from "
        "KML that would otherwise be rejected by shapefile/GDB writers.",
    )

    selected_cols = None
    if allow_columns:
        attr_cols = [c for c in gdf.columns if c != gdf.geometry.name]
        if attr_cols:
            with st.expander(f"Columns ({len(attr_cols)} available)"):
                selected_cols = st.multiselect(
                    "Include columns in output",
                    attr_cols,
                    default=attr_cols,
                    key=f"{key_prefix}_cols",
                )

    return {
        "output_format": output_format,
        "crs_choice": crs_choice,
        "custom_epsg": custom_epsg,
        "fix_invalid": fix_invalid,
        "selected_cols": selected_cols,
    }


def _render_map_preview(gdf: gpd.GeoDataFrame) -> None:
    try:
        map_gdf = gdf
        if map_gdf.crs is not None and map_gdf.crs.to_epsg() != 4326:
            map_gdf = map_gdf.to_crs(4326)
        map_gdf = map_gdf[
            map_gdf.geometry.notna() & ~map_gdf.geometry.is_empty
        ]

        if len(map_gdf) == 0:
            st.info("No geometries available to preview.")
            return

        if len(map_gdf) > MAP_FEATURE_LIMIT:
            st.caption(
                f"Showing the first {MAP_FEATURE_LIMIT:,} of "
                f"{len(map_gdf):,} features for performance.",
            )
            map_gdf = map_gdf.head(MAP_FEATURE_LIMIT)

        # Try to include attributes for tooltips; fall back to geom-only
        # if the frame contains non-JSON-serialisable types.
        try:
            geojson = json.loads(map_gdf.to_json(default=str))
        except Exception:
            geom_only = gpd.GeoDataFrame(
                geometry=map_gdf.geometry,
                crs=map_gdf.crs,
            )
            geojson = json.loads(geom_only.to_json())

        minx, miny, maxx, maxy = map_gdf.total_bounds
        center_lon = float((minx + maxx) / 2)
        center_lat = float((miny + maxy) / 2)
        span = max(maxx - minx, maxy - miny, 1e-4)
        zoom = max(0.0, min(18.0, math.log2(360.0 / span) - 1.0))

        layer = pdk.Layer(
            "GeoJsonLayer",
            data=geojson,
            pickable=True,
            stroked=True,
            filled=True,
            extruded=False,
            get_fill_color=[255, 140, 0, 120],
            get_line_color=[200, 30, 0, 220],
            get_line_width=2,
            line_width_min_pixels=1,
            point_radius_min_pixels=4,
            get_point_radius=40,
        )

        # Build a tooltip template from up to 6 non-geometry columns.
        tooltip = None
        attr_cols = [
            c for c in map_gdf.columns if c != map_gdf.geometry.name
        ][:6]
        if attr_cols:
            rows = "".join(
                f"<div><b>{c}:</b> {{{c}}}</div>" for c in attr_cols
            )
            tooltip = {
                "html": rows,
                "style": {
                    "backgroundColor": "white",
                    "color": "#333",
                    "fontSize": "12px",
                    "padding": "6px",
                    "border": "1px solid #ccc",
                },
            }

        st.pydeck_chart(
            pdk.Deck(
                layers=[layer],
                initial_view_state=pdk.ViewState(
                    longitude=center_lon,
                    latitude=center_lat,
                    zoom=float(zoom),
                ),
                tooltip=tooltip,
                map_style=pdk.map_styles.LIGHT,
            ),
        )
    except Exception as exc:
        st.info(f"Unable to render map preview ({exc}).")


# --- Sidebar ---
with st.sidebar:
    st.header("🌎 Data source")

    source = st.radio(
        "Input type",
        ["Upload file(s)", "ArcGIS feature layer URL", "Paste WKT"],
        key="source_type",
    )

    uploaded_files: list = []
    arcgis_url = ""
    wkt_text = ""
    if source == "Upload file(s)":
        uploaded_files = st.file_uploader(
            "Geospatial file(s)",
            type=["kml", "kmz", "geojson", "json", "zip", "wkt", "gpx"],
            help=INPUT_FORMAT_HELP,
            accept_multiple_files=True,
        ) or []
    elif source == "ArcGIS feature layer URL":
        arcgis_url = st.text_input(
            "Feature layer URL",
            placeholder=(
                "https://maps1.vcgov.org/arcgis/rest/services/Beaches/MapServer/6"
            ),
        )
    else:  # Paste WKT
        wkt_text = st.text_area(
            "WKT geometries",
            placeholder="POINT (30 10)\nLINESTRING (30 10, 10 30, 40 40)",
            height=160,
            help="One WKT geometry per line. Blank lines and '#' comments are ignored.",
        )

    load_clicked = st.button(
        "Load data",
        type="primary",
        use_container_width=True,
    )

    if st.session_state.datasets:
        if st.button("Clear", use_container_width=True):
            _clear_all()
            st.rerun()


# --- Load ---
if load_clicked:
    _clear_all()
    new_datasets: list = []
    try:
        if source == "ArcGIS feature layer URL":
            if not arcgis_url:
                st.session_state.load_error = (
                    "Enter a feature layer URL before loading."
                )
            else:
                with st.spinner("Fetching feature layer…"):
                    name, gdf = asyncio.run(get_arcgis_data(arcgis_url))
                new_datasets.append({"name": name, "gdf": gdf})
        elif source == "Paste WKT":
            if not wkt_text.strip():
                st.session_state.load_error = (
                    "Paste at least one WKT geometry before loading."
                )
            else:
                with st.spinner("Parsing WKT…"):
                    gdf = read_wkt_text(wkt_text)
                if len(gdf) == 0:
                    st.session_state.load_error = (
                        "No valid WKT geometries were parsed."
                    )
                else:
                    new_datasets.append({"name": "wkt_input", "gdf": gdf})
        else:  # Upload file(s)
            if not uploaded_files:
                st.session_state.load_error = (
                    "Choose one or more files before loading."
                )
            else:
                for uf in uploaded_files:
                    with st.spinner(f"Reading {uf.name}…"):
                        name, _ = os.path.splitext(
                            os.path.basename(uf.name),
                        )
                        new_datasets.append(
                            {"name": name, "gdf": read_file(uf)},
                        )
        if not st.session_state.load_error:
            st.session_state.datasets = new_datasets
    except Exception as exc:
        st.session_state.load_error = f"Failed to load data: {exc}"


# --- Main area ---
st.title(f"🌎 {APP_NAME}")
st.caption(f"v{__version__} — convert between common geospatial formats")

if st.session_state.load_error:
    st.error(st.session_state.load_error)

datasets = st.session_state.datasets or []

if not datasets:
    st.info(
        "Use the sidebar to upload a file, paste WKT, or provide an ArcGIS "
        "feature layer URL, then click **Load data**. You can upload "
        "multiple files to batch-convert them in one shot.",
    )
    with st.expander("Supported input formats"):
        st.markdown(
            "- **KML / KMZ** — Google Earth\n"
            "- **GeoJSON** (.geojson) — RFC 7946\n"
            "- **Esri Feature JSON** (.json) — ArcGIS Pro / ArcGIS REST\n"
            "- **WKT** (.wkt file or pasted text) — one geometry per line\n"
            "- **GPX** (.gpx) — GPS tracks, routes, waypoints\n"
            "- **ZIP** — shapefile or file geodatabase\n"
            "- **ArcGIS feature layer URL**",
        )
elif len(datasets) == 1:
    # --- Single-dataset flow ---
    ds = datasets[0]
    gdf = ds["gdf"]
    fn = ds["name"]

    st.subheader(fn or "Loaded dataset")

    c1, c2, c3 = st.columns(3)
    c1.metric("Features", f"{len(gdf):,}")
    c2.metric("Attributes", f"{max(gdf.shape[1] - 1, 0):,}")
    c3.metric("CRS", str(gdf.crs) if gdf.crs else "unknown")

    try:
        geom_types = gdf.geometry.geom_type.value_counts()
        if len(geom_types):
            st.caption(
                "Geometry types: "
                + ", ".join(f"{t} ({n:,})" for t, n in geom_types.items()),
            )
    except Exception:
        pass

    st.divider()

    preview_col, convert_col = st.columns([2, 1])

    with convert_col:
        st.markdown("### Convert")
        choices = _render_convert_controls(gdf, allow_columns=True, key_prefix="single")

        if st.button("Convert", type="primary", use_container_width=True):
            try:
                target_crs = _resolve_target_crs(
                    choices["crs_choice"], choices["custom_epsg"], gdf,
                )
                transformed = _transform_gdf(
                    gdf,
                    target_crs=target_crs,
                    columns=choices["selected_cols"],
                    fix_invalid=choices["fix_invalid"],
                )
                file_ext, dl_ext, mimetype = output_format_dict[choices["output_format"]]
                output_fn = f"{fn}.{file_ext}"
                dl_fn = f"{fn}.{dl_ext}"
                with st.spinner(f"Converting to {choices['output_format']}…"):
                    converted = convert(
                        gdf=transformed,
                        output_name=output_fn,
                        output_format=choices["output_format"],
                    )
                st.session_state.converted_data = converted
                st.session_state.converted_fn = dl_fn
                st.session_state.converted_mime = mimetype
            except Exception as exc:
                _reset_converted()
                st.error(f"Conversion failed: {exc}")

        if st.session_state.converted_data is not None:
            size_kb = len(st.session_state.converted_data) / 1024
            st.success(
                f"Ready: **{st.session_state.converted_fn}** ({size_kb:,.1f} KB)",
            )
            st.download_button(
                label=f"⬇️ Download {st.session_state.converted_fn}",
                data=st.session_state.converted_data,
                file_name=st.session_state.converted_fn,
                mime=st.session_state.converted_mime,
                use_container_width=True,
            )

    with preview_col:
        attr_tab, map_tab = st.tabs(["Attributes", "Map preview"])
        with attr_tab:
            geom_name = gdf.geometry.name
            display_df = gdf.drop(columns=[geom_name]).rename(columns=str)
            try:
                st.dataframe(
                    display_df,
                    use_container_width=True,
                    height=400,
                )
            except Exception as exc:
                st.warning(f"Unable to render attribute table ({exc}).")
                st.dataframe(display_df.astype(str), use_container_width=True)
        with map_tab:
            _render_map_preview(gdf)
else:
    # --- Batch mode ---
    st.subheader(f"{len(datasets)} datasets loaded")

    summary_rows = []
    for ds in datasets:
        g = ds["gdf"]
        try:
            geom_types = sorted(g.geometry.geom_type.dropna().unique().tolist())
        except Exception:
            geom_types = []
        summary_rows.append(
            {
                "name": ds["name"],
                "features": len(g),
                "attributes": max(g.shape[1] - 1, 0),
                "crs": str(g.crs) if g.crs else "unknown",
                "geom_types": ", ".join(geom_types),
            },
        )
    st.dataframe(summary_rows, use_container_width=True, hide_index=True)

    st.divider()

    convert_col, _ = st.columns([1, 1])
    with convert_col:
        st.markdown("### Batch convert")
        st.caption(
            "All datasets are converted with the same options and bundled "
            "into a single download (.zip).",
        )
        # Column selection doesn't apply in batch (schemas may differ).
        choices = _render_convert_controls(
            datasets[0]["gdf"], allow_columns=False, key_prefix="batch",
        )

        if st.button(
            "Convert all", type="primary", use_container_width=True,
        ):
            try:
                output_format = choices["output_format"]
                file_ext, dl_ext, _mimetype = output_format_dict[output_format]

                batch_buffer = io.BytesIO()
                with zipfile.ZipFile(
                    batch_buffer, "w", zipfile.ZIP_DEFLATED,
                ) as outer_zip:
                    for ds in datasets:
                        name = ds["name"]
                        target_crs = _resolve_target_crs(
                            choices["crs_choice"],
                            choices["custom_epsg"],
                            ds["gdf"],
                        )
                        transformed = _transform_gdf(
                            ds["gdf"],
                            target_crs=target_crs,
                            columns=None,
                            fix_invalid=choices["fix_invalid"],
                        )
                        with st.spinner(f"Converting {name}…"):
                            out_bytes = convert(
                                gdf=transformed,
                                output_name=f"{name}.{file_ext}",
                                output_format=output_format,
                            )
                        # For formats that are themselves zips (shapefile,
                        # gdb), unpack into a subdirectory instead of
                        # nesting zips.
                        if dl_ext == "zip":
                            with zipfile.ZipFile(
                                io.BytesIO(out_bytes),
                            ) as inner:
                                for info in inner.infolist():
                                    outer_zip.writestr(
                                        f"{name}/{info.filename}",
                                        inner.read(info.filename),
                                    )
                        else:
                            outer_zip.writestr(f"{name}.{dl_ext}", out_bytes)

                st.session_state.converted_data = batch_buffer.getvalue()
                st.session_state.converted_fn = (
                    f"converted_{len(datasets)}_datasets.zip"
                )
                st.session_state.converted_mime = "application/zip"
            except Exception as exc:
                _reset_converted()
                st.error(f"Batch conversion failed: {exc}")

        if st.session_state.converted_data is not None:
            size_kb = len(st.session_state.converted_data) / 1024
            st.success(
                f"Ready: **{st.session_state.converted_fn}** ({size_kb:,.1f} KB)",
            )
            st.download_button(
                label=f"⬇️ Download {st.session_state.converted_fn}",
                data=st.session_state.converted_data,
                file_name=st.session_state.converted_fn,
                mime=st.session_state.converted_mime,
                use_container_width=True,
            )
