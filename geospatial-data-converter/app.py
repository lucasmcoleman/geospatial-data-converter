import asyncio
import json
import math
import os

import geopandas as gpd
import pydeck as pdk
import streamlit as st
from aiohttp import ClientSession
from restgdf import FeatureLayer

from utils import read_file, convert, output_format_dict

__version__ = "1.1.0"
APP_NAME = "Geospatial Data Converter"


INPUT_FORMAT_HELP = (
    "Supported uploads: KML, KMZ, GeoJSON (.geojson), "
    "Esri Feature JSON (.json), WKT (.wkt), "
    "ZIP (shapefile or file geodatabase)."
)

OUTPUT_FORMAT_HELP = {
    "CSV": "Comma-separated values; geometry is serialised as WKT.",
    "KML": "Google Earth Keyhole Markup Language.",
    "GeoJSON": "RFC 7946 GeoJSON (text).",
    "TopoJSON": "Topology-preserving JSON.",
    "WKT": "One Well-Known Text geometry per line.",
    "EsriJSON": "Esri Feature JSON (FeatureSet) as consumed by ArcGIS Pro.",
    "ESRI Shapefile": "Zipped shapefile (.shp, .shx, .dbf, .prj).",
    "OpenFileGDB": "Zipped File Geodatabase.",
}


def st_init_null(*variable_names) -> None:
    for variable_name in variable_names:
        if variable_name not in st.session_state:
            st.session_state[variable_name] = None


st_init_null(
    "fn_without_extension",
    "gdf",
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
    st.session_state.gdf = None
    st.session_state.fn_without_extension = None
    st.session_state.load_error = None
    _reset_converted()


# --- Sidebar: data source ---
with st.sidebar:
    st.header("🌎 Data source")

    source = st.radio(
        "Input type",
        ["Upload a file", "ArcGIS feature layer URL"],
        key="source_type",
        horizontal=False,
    )

    uploaded_file = None
    arcgis_url = ""
    if source == "Upload a file":
        uploaded_file = st.file_uploader(
            "Geospatial file",
            type=["kml", "kmz", "geojson", "json", "zip", "wkt"],
            help=INPUT_FORMAT_HELP,
        )
    else:
        arcgis_url = st.text_input(
            "Feature layer URL",
            placeholder=(
                "https://maps1.vcgov.org/arcgis/rest/services/Beaches/MapServer/6"
            ),
        )

    load_clicked = st.button(
        "Load data",
        type="primary",
        use_container_width=True,
    )

    if st.session_state.gdf is not None:
        if st.button("Clear", use_container_width=True):
            _clear_all()
            st.rerun()


# --- Load on click ---
if load_clicked:
    _clear_all()
    try:
        if source == "ArcGIS feature layer URL":
            if not arcgis_url:
                st.session_state.load_error = (
                    "Enter a feature layer URL before loading."
                )
            else:
                with st.spinner("Fetching feature layer…"):
                    name, gdf = asyncio.run(get_arcgis_data(arcgis_url))
                st.session_state.fn_without_extension = name
                st.session_state.gdf = gdf
        else:
            if uploaded_file is None:
                st.session_state.load_error = (
                    "Choose a file before loading."
                )
            else:
                with st.spinner(f"Reading {uploaded_file.name}…"):
                    st.session_state.fn_without_extension, _ = os.path.splitext(
                        os.path.basename(uploaded_file.name),
                    )
                    st.session_state.gdf = read_file(uploaded_file)
    except Exception as exc:
        st.session_state.load_error = f"Failed to load data: {exc}"


# --- Main area ---
st.title(f"🌎 {APP_NAME}")
st.caption(f"v{__version__} — convert between common geospatial formats")

if st.session_state.load_error:
    st.error(st.session_state.load_error)

if st.session_state.gdf is None:
    st.info(
        "Use the sidebar to upload a file or paste an ArcGIS feature layer URL, "
        "then click **Load data**.",
    )
    with st.expander("Supported input formats"):
        st.markdown(
            "- **KML / KMZ** — Google Earth\n"
            "- **GeoJSON** (.geojson) — RFC 7946\n"
            "- **Esri Feature JSON** (.json) — ArcGIS Pro / ArcGIS REST\n"
            "- **WKT** (.wkt) — one Well-Known Text geometry per line\n"
            "- **ZIP** — shapefile or file geodatabase\n"
            "- **ArcGIS feature layer URL**",
        )
else:
    gdf = st.session_state.gdf
    st.subheader(st.session_state.fn_without_extension or "Loaded dataset")

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
        output_format = st.selectbox(
            "Output format",
            list(output_format_dict.keys()),
            key="output_format",
        )
        st.caption(OUTPUT_FORMAT_HELP.get(output_format, ""))

        if st.button("Convert", type="primary", use_container_width=True):
            try:
                file_ext, dl_ext, mimetype = output_format_dict[output_format]
                output_fn = (
                    f"{st.session_state.fn_without_extension}.{file_ext}"
                )
                dl_fn = f"{st.session_state.fn_without_extension}.{dl_ext}"
                with st.spinner(f"Converting to {output_format}…"):
                    converted = convert(
                        gdf=gdf,
                        output_name=output_fn,
                        output_format=output_format,
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
            display_df = gdf.drop(columns=[geom_name])
            # PyArrow (used by st.dataframe) chokes on non-string column
            # labels, so coerce to str for display.
            display_df = display_df.rename(columns=str)
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
            try:
                map_gdf = gdf
                if map_gdf.crs is not None and map_gdf.crs.to_epsg() != 4326:
                    map_gdf = map_gdf.to_crs(4326)
                map_gdf = map_gdf[
                    map_gdf.geometry.notna() & ~map_gdf.geometry.is_empty
                ]

                if len(map_gdf) == 0:
                    st.info("No geometries available to preview.")
                else:
                    # Build a GeoJSON with geometry only — avoids serialising
                    # attribute columns that may contain non-JSON-safe types.
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
                        pickable=False,
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

                    view_state = pdk.ViewState(
                        longitude=center_lon,
                        latitude=center_lat,
                        zoom=float(zoom),
                    )

                    st.pydeck_chart(
                        pdk.Deck(
                            layers=[layer],
                            initial_view_state=view_state,
                            map_style=pdk.map_styles.LIGHT,
                        ),
                    )
            except Exception as exc:
                st.info(f"Unable to render map preview ({exc}).")
