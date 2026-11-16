"""Where a GIS file says it is: its coordinate system and its extent.

ADMA reads both from the file itself before it publishes the file to GeoServer,
and never guesses. A file whose coordinate system or extent cannot be read is
an error the user is told about, not a layer placed somewhere invented.

The coordinate system is kept as an EPSG code, the form GeoServer and the map
pages understand; an ESRI .prj is matched to its EPSG code. The extent is kept
in longitude and latitude (EPSG:4326), the form both the single file map and
the custom Map pages zoom to.

Vectors are read with pyogrio and rasters with rasterio; both bring their own
GDAL.

A raw drone band photo is a TIF too, but not a map: it has no coordinate
system, only the GPS point where the drone was. It is told apart by what the
camera wrote into it, not by its name, and reported as DronePhoto with that
point, so the pages can explain it and pin it instead of calling it an error.
"""
import dataclasses
import json
import math
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

RASTER_EXTENSIONS = ('.tif', '.tiff', '.geotif', '.geotiff')
# Vector formats GeoServer has no store for: publishing converts them to a GeoPackage.
CONVERTED_EXTENSIONS = ('.geojson', '.gpkg', '.kml', '.kmz')
# The parts of a shapefile ADMA needs to map it. Without the .prj there is no
# coordinate system to read.
SHAPEFILE_REQUIRED = ('.shp', '.shx', '.dbf', '.prj')
# Parts used when present: the encoding of the .dbf and the spatial indexes.
SHAPEFILE_OPTIONAL = ('.cpg', '.qix', '.sbn', '.sbx')
SHAPEFILE_PARTS = SHAPEFILE_REQUIRED + SHAPEFILE_OPTIONAL

# How sure pyproj must be that a coordinate system is a given EPSG code when
# the exact match finds none. A lower match is taken only when it is the one
# best match.
FALLBACK_CONFIDENCE = 25

# The cameras whose raw band photos are on ADAPT, by the TIFF Make tag they
# write and the XMP namespace of their own metadata. A TIF with no
# georeferencing that carries either is a drone photo.
DRONE_CAMERAS = (
    ('DJI', 'http://www.dji.com/drone-dji/1.0/'),
    ('MicaSense', 'http://micasense.com/MicaSense/1.0/'),
)
TIFF_MAKE, TIFF_MODEL, TIFF_XMP, EXIF_GPS = 271, 272, 700, 0x8825
GPS_LATITUDE_REF, GPS_LATITUDE, GPS_LONGITUDE_REF, GPS_LONGITUDE, GPS_ALTITUDE_REF, GPS_ALTITUDE = 1, 2, 3, 4, 5, 6

NO_CRS = 'The raster has no coordinate system'


class GisReadError(Exception):
    """ADMA cannot place this file on the map. The message says why, in words for the user."""


class ShapefilePartsMissing(GisReadError):
    """A shapefile whose other parts are not there (yet): they may still be on their way up."""

    def __init__(self, missing):
        self.missing = list(missing)
        super().__init__(f"The shapefile has no {', '.join(self.missing)} next to it")


@dataclass(frozen=True)
class DronePhotoInfo:
    """What a raw drone photo says about itself: its camera and where the drone was.

    latitude and longitude are None when the photo holds no GPS fix.
    relative_altitude is the height above the takeoff point (DJI) and
    gps_altitude the GPS height above sea level, in meters. gimbal_pitch is the
    camera angle in degrees, 0 level and -90 straight down (DJI).
    """

    camera: str
    band: str = None
    latitude: float = None
    longitude: float = None
    relative_altitude: float = None
    gps_altitude: float = None
    gimbal_pitch: float = None

    @classmethod
    def from_json(cls, data):
        """The DronePhotoInfo that File.drone_photo keeps."""
        names = {field.name for field in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in names})

    def as_json(self):
        """The photo as File.drone_photo keeps it."""
        return dataclasses.asdict(self)

    @property
    def has_position(self):
        return self.latitude is not None and self.longitude is not None

    @property
    def position_text(self):
        """'42.09141, -101.45998': latitude and longitude to about a meter, as map apps take them."""
        return f'{self.latitude:.5f}, {self.longitude:.5f}' if self.has_position else None

    @property
    def height_text(self):
        if self.relative_altitude is not None:
            return f'{self.relative_altitude:.1f} m above takeoff'
        if self.gps_altitude is not None:
            return f'{self.gps_altitude:,.0f} m above sea level'
        return None

    @property
    def camera_angle_text(self):
        if self.gimbal_pitch is None:
            return None
        below = round(-self.gimbal_pitch)
        if below >= 90:
            return 'Straight down'
        if below == 0:
            return 'Level'
        if below < 0:
            return f'{-below}° above horizontal'
        return f'{below}° below horizontal'

    def describe(self):
        """'DJI M3M, Green band': the camera, and the band when the photo names it."""
        return self.camera + (f', {self.band} band' if self.band else '')


class DronePhoto(GisReadError):
    """A raw drone photo: not a map, only the point where the drone was (DronePhotoInfo)."""

    def __init__(self, photo):
        self.photo = photo
        super().__init__(f'Raw drone photo, not a map ({photo.describe()})')


@dataclass(frozen=True)
class SpatialInfo:
    """The EPSG code of a file's coordinate system, and its extent in longitude and latitude."""

    epsg: int
    lonlat: tuple  # (west, south, east, north)

    @property
    def crs(self):
        return f'EPSG:{self.epsg}'

    def envelope_json(self):
        """The extent as File.spatial_extent keeps it."""
        west, south, east, north = self.lonlat
        return json.dumps({
            'type': 'envelope',
            'crs': 'EPSG:4326',
            'coordinates': [[west, south], [east, north]],
        })


def kind_of(name):
    """'raster', 'shapefile', 'zip' or 'vector' for a file ADMA can map, else None."""
    ext = Path(name).suffix.lower()
    if ext in RASTER_EXTENSIONS:
        return 'raster'
    if ext == '.shp':
        return 'shapefile'
    if ext == '.zip':
        return 'zip'
    if ext in CONVERTED_EXTENSIONS:
        return 'vector'
    return None


def epsg_code(crs):
    """The EPSG code of coordinate system `crs` (WKT, ESRI WKT, or anything pyproj reads).

    GisReadError when pyproj cannot read it or cannot match it to one EPSG code.
    """
    from pyproj import CRS
    from pyproj.exceptions import CRSError

    try:
        crs = CRS.from_user_input(crs)
    except CRSError:
        raise GisReadError('ADMA cannot read the coordinate system of this file')
    code = crs.to_epsg()
    if code is not None:
        return code
    matches = crs.list_authority(auth_name='EPSG', min_confidence=FALLBACK_CONFIDENCE)
    best = [m for m in matches if m.confidence == matches[0].confidence] if matches else []
    if len(best) == 1:
        return int(best[0].code)
    raise GisReadError(f'ADMA cannot match the coordinate system "{crs.name}" to an EPSG code')


def read(path, name=None):
    """The SpatialInfo of the GIS file at `path`; `name` gives its type when the path does not.

    A shapefile is read from `path` with its other parts beside it under the
    same name. GisReadError says why a file cannot be mapped.
    """
    kind = kind_of(name or path)
    if kind == 'raster':
        return read_raster(path)
    if kind == 'zip':
        return read_vector(f'/vsizip/{path}/{zip_shapefile(path)}')
    if kind in ('shapefile', 'vector'):
        return read_vector(path)
    raise GisReadError(f'ADMA cannot show {Path(name or path).suffix or "this kind of"} files on a map')


def read_vector(path):
    """The SpatialInfo of a vector file holding one layer."""
    import pyogrio

    try:
        layers = pyogrio.list_layers(path)
        if len(layers) != 1:
            raise GisReadError(f'The file holds {len(layers)} layers; ADMA maps a file with exactly one layer')
        info = pyogrio.read_info(path, force_total_bounds=True)
    except GisReadError:
        raise
    except Exception as exc:
        raise GisReadError(f'ADMA cannot read this file: {exc}')
    if not info.get('crs'):
        raise GisReadError('The file has no coordinate system (a shapefile needs its .prj)')
    if not info.get('features'):
        raise GisReadError('The file holds no features')
    return _info(epsg_code(info['crs']), info.get('total_bounds'))


def read_raster(path):
    """The SpatialInfo of a raster; DronePhoto for a raw drone photo."""
    import rasterio

    try:
        with rasterio.open(path) as src:
            crs, bounds = src.crs, tuple(src.bounds)
            georeferenced = bool(src.gcps[0]) or bool(src.rpcs) or not src.transform.is_identity
    except Exception as exc:
        raise GisReadError(f'ADMA cannot read this raster: {exc}')
    if crs is None:
        photo = None if georeferenced else drone_photo(path)
        if photo is not None:
            raise DronePhoto(photo)
        raise GisReadError(NO_CRS)
    return _info(epsg_code(crs.to_wkt()), bounds)


def drone_photo(path):
    """The DronePhotoInfo of the TIF at `path` if a drone camera made it, else None.

    Read with Pillow, which gives the TIFF Make and Model tags (GDAL reports
    neither), the XMP packet and the EXIF GPS block. A DJI photo keeps its GPS
    point, height and camera angle in its XMP; a MicaSense photo only in EXIF.
    """
    from PIL import Image

    try:
        with Image.open(path) as image:
            tags = image.tag_v2
            make, model, xmp = (_text(tags.get(tag)) for tag in (TIFF_MAKE, TIFF_MODEL, TIFF_XMP))
            gps = dict(image.getexif().get_ifd(EXIF_GPS))
    except Exception:
        # Not a TIFF Pillow can read: nothing says a camera made it.
        return None
    brand = next((name for name, namespace in DRONE_CAMERAS
                  if make.casefold() == name.casefold() or namespace in xmp), None)
    if brand is None:
        return None

    latitude = _number(_xmp(xmp, 'drone-dji', 'GpsLatitude'))
    # Older DJI firmware spells it GpsLongtitude.
    longitude = _number(_xmp(xmp, 'drone-dji', 'GpsLongitude', 'GpsLongtitude'))
    if latitude is None or longitude is None:
        latitude = _exif_degrees(gps.get(GPS_LATITUDE), gps.get(GPS_LATITUDE_REF))
        longitude = _exif_degrees(gps.get(GPS_LONGITUDE), gps.get(GPS_LONGITUDE_REF))
    # A drone with no GPS fix writes 0, 0.
    if (latitude is None or longitude is None or not (-90 <= latitude <= 90 and -180 <= longitude <= 180)
            or latitude == longitude == 0):
        latitude = longitude = None
    gps_altitude = _number(gps.get(GPS_ALTITUDE))
    if gps_altitude is not None and gps.get(GPS_ALTITUDE_REF) in (1, b'\x01'):
        gps_altitude = -gps_altitude
    return DronePhotoInfo(
        camera=' '.join(part for part in (make or brand, model) if part),
        band=_xmp(xmp, 'Camera', 'BandName'),
        latitude=latitude,
        longitude=longitude,
        relative_altitude=_number(_xmp(xmp, 'drone-dji', 'RelativeAltitude')),
        gps_altitude=gps_altitude,
        gimbal_pitch=_number(_xmp(xmp, 'drone-dji', 'GimbalPitchDegree')),
    )


def _text(value):
    """A TIFF text or byte tag as a str, without the padding cameras leave."""
    if isinstance(value, bytes):
        value = value.decode('utf-8', 'replace')
    return (value or '').strip('\x00 \t\r\n') if isinstance(value, str) else ''


def _xmp(xmp, prefix, *names):
    """The text of XMP property prefix:name, written as an attribute or an element (the first item of a list)."""
    for name in names:
        match = re.search(
            rf'\b{re.escape(prefix)}:{name}(?:\s*=\s*"([^"]*)"|\s*>\s*(?:<rdf:(?:Seq|Bag|Alt)>\s*<rdf:li[^>]*>)?([^<]*))',
            xmp)
        if match:
            value = (match.group(1) if match.group(1) is not None else match.group(2)).strip()
            if value:
                return value
    return None


def _number(value):
    """A finite float from an XMP string ('+20.348') or an EXIF rational, else None."""
    try:
        number = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return number if math.isfinite(number) else None


def _exif_degrees(dms, ref):
    """Decimal degrees from EXIF degrees, minutes and seconds, negative to the south and west."""
    try:
        degrees, minutes, seconds = (_number(part) for part in dms)
    except (TypeError, ValueError):
        return None
    if None in (degrees, minutes, seconds):
        return None
    value = degrees + minutes / 60 + seconds / 3600
    return -value if _text(ref).upper() in ('S', 'W') else value


def zip_shapefile(path):
    """The name of the one shapefile in zip archive `path`, or GisReadError."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError):
        raise GisReadError('The file is not a zip archive ADMA can open')
    shapefiles = [n for n in names if n.lower().endswith('.shp') and not n.startswith('__MACOSX/')]
    if not shapefiles:
        raise GisReadError('The zip archive holds no shapefile')
    if len(shapefiles) > 1:
        raise GisReadError(f'The zip archive holds {len(shapefiles)} shapefiles; ADMA maps a zip with exactly one')
    shp = shapefiles[0]
    stem = shp[:-4].lower()
    present = {n[len(stem):].lower() for n in names if n.lower().startswith(stem) and len(n) > len(stem)}
    missing = [ext for ext in SHAPEFILE_REQUIRED if ext not in present]
    if missing:
        raise GisReadError(f"The shapefile in the zip archive has no {', '.join(missing)}")
    return shp


def _info(epsg, bounds):
    from pyproj import Transformer

    if not bounds or not all(math.isfinite(v) for v in bounds):
        raise GisReadError('ADMA cannot read the extent of this file')
    try:
        to_lonlat = Transformer.from_crs(epsg, 4326, always_xy=True)
        west, south, east, north = to_lonlat.transform_bounds(*bounds, densify_pts=21)
    except Exception:
        raise GisReadError(f'The extent of this file does not fit its coordinate system EPSG:{epsg}')
    if not (all(math.isfinite(v) for v in (west, south, east, north))
            and -180 <= west <= east <= 180 and -90 <= south <= north <= 90):
        raise GisReadError(f'The extent of this file does not fit its coordinate system EPSG:{epsg}')
    return SpatialInfo(epsg=epsg, lonlat=(west, south, east, north))
