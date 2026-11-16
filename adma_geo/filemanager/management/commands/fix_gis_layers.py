"""Read spatial files again where ADMA once guessed, and repair GeoServer layers WMS cannot draw.

Before ADMA read GIS files, processing stored a guess: EPSG:4326 and the whole
world for vectors, EPSG:32614 and a 2 km box near Mead for rasters. And an ESRI
.prj (a State Plane shapefile, say) left its GeoServer layer with srs null and
disabled, so WMS answered LayerNotDefined and the file ended in error. This
command finds both:

* A spatial file in 'error', or whose spatial_extent was not read from the file
  (only the reading code marks the extent "crs": "EPSG:4326"), is processed
  again: its coordinate system and extent are read from its bytes. Then it is
  published, or its layer repaired if it has one already.
* A file with real metadata whose layer has srs null, is disabled or has no
  bounding box gets the EPSG code read from the file (FORCE_DECLARED), is
  enabled and its bounding boxes recalculated. A layer GeoServer has lost, or a
  file processed but never published, is published again.
* A vector layer still drawn in one of GeoServer's generic default styles
  (polygon, line, point: an opaque gray block over the imagery) is given
  ADMA's vector style. Rasters, and layers given any other style, keep theirs.

Files still 'pending' or 'processing' are left alone: ADAPT files are processed
when someone opens their map. So are raw drone photos ('drone_photo'): they
were read, and are not maps.

Then every custom Map's layer group is brought in step with its visible layers,
through the same update_layer_group the map pages use: a group made before
ADMA read extents keeps the whole world as its bounds, and a map whose group
GeoServer lost gets it again.

It prints one line per spatial file and one per Map. Running it again changes
nothing more, except that a file still in error is tried again.

Usage:
    python manage.py fix_gis_layers            # dry run: report only
    python manage.py fix_gis_layers --apply    # make the changes
"""
import json

from django.core.management.base import BaseCommand

from filemanager import adapt_records, gis_read
from filemanager.geoserver_layer_group_manager import LayerGroupManager
from filemanager.geoserver_manager import SystematicGeoServerManager, epsg_of
from filemanager.gis_utils import process_gis_file
from filemanager.models import File, Map


def read_from_file(file_obj):
    """True if file_obj's spatial_extent was read from the file, not guessed."""
    try:
        return json.loads(file_obj.spatial_extent).get('crs') == 'EPSG:4326'
    except (TypeError, ValueError, AttributeError):
        return False


class Command(BaseCommand):
    help = ('Process spatial files again where ADMA guessed their coordinate system or extent, or failed, '
            'repair GeoServer layers with srs null or disabled, and bring custom maps\' layer groups '
            'in step with their layers. Dry run unless --apply.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Make the changes (default: report only)')

    def handle(self, *args, **options):
        self.apply = options['apply']
        self.manager = SystematicGeoServerManager()
        counts = {}
        for file_obj in File.objects.filter(is_spatial=True).order_by('created_at', 'pk'):
            try:
                outcome, detail = self.fix(file_obj)
            except Exception as exc:
                # GeoServer unreachable, say: report it and go on with the next file.
                outcome, detail = 'failed', str(exc)
            counts[outcome] = counts.get(outcome, 0) + 1
            self.stdout.write(f'{file_obj.id} {file_obj.name}: {outcome}' + (f' ({detail})' if detail else ''))
        summary = ', '.join(f'{count} {outcome}' for outcome, count in sorted(counts.items())) or 'no spatial files'

        # After the files, so a group takes the extents and layers just repaired
        self.groups = LayerGroupManager()
        map_counts = {}
        for map_obj in Map.objects.filter(deletion_in_progress=False).order_by('created_at', 'pk'):
            try:
                outcome, detail = self.fix_map(map_obj)
            except Exception as exc:
                outcome, detail = 'failed', str(exc)
            map_counts[outcome] = map_counts.get(outcome, 0) + 1
            self.stdout.write(f'Map {map_obj.id} {map_obj.name}: {outcome}' + (f' ({detail})' if detail else ''))
        summary += '; maps: ' + (', '.join(f'{count} {outcome}' for outcome, count in sorted(map_counts.items()))
                                 or 'none')
        self.stdout.write(('' if self.apply else 'Dry run: ') + summary)

    def fix(self, file_obj):
        """(outcome, detail) for one file, after changing it when --apply."""
        if file_obj.gis_status in ('pending', 'processing'):
            return 'left alone', file_obj.gis_status
        if file_obj.gis_status == 'drone_photo':
            return 'left alone', 'raw drone photo, not a map'
        if adapt_records.is_gone(file_obj):
            return 'left alone', 'no longer on the ADAPT share'
        if gis_read.kind_of(file_obj.name) is None:
            return 'left alone', 'not a type ADMA maps'

        if file_obj.gis_status == 'error' or not read_from_file(file_obj):
            reason = 'error' if file_obj.gis_status == 'error' else f'guessed {file_obj.crs or "no crs"}'
            if not self.apply:
                return 'would reprocess', reason
            return self.reprocess(file_obj)

        problem = self.layer_problem(file_obj)
        if problem is None:
            style = self.style_problem(file_obj)
            if style is None:
                return 'ok', file_obj.crs
            if not self.apply:
                return 'would restyle', style
            return self.restyle(file_obj, style)
        if not self.apply:
            return 'would repair', problem
        return self.repair(file_obj, problem)

    def fix_map(self, map_obj):
        """(outcome, detail) for one Map's layer group, after refreshing it when --apply."""
        change = self.groups.layer_group_change(map_obj)
        if change is None:
            return 'ok', None
        if not self.apply:
            return 'would refresh', change
        success, message = self.groups.update_layer_group(map_obj)
        return ('refreshed' if success else 'error'), f'{change}: {message}'

    def layer_problem(self, file_obj):
        if file_obj.gis_status != 'published' or not file_obj.geoserver_layer_name:
            return 'not published'
        return self.manager.layer_problem(*self.layer_of(file_obj))

    def style_problem(self, file_obj):
        """Why a vector layer needs ADMA's style ('GeoServer default style polygon', say), or None."""
        store, layer, resource = self.layer_of(file_obj)
        if resource != 'featureType':
            return None
        return self.manager.vector_style_problem(layer)

    def restyle(self, file_obj, style):
        """Give the file's vector layer ADMA's style, as publishing does now."""
        success, message = self.manager.use_vector_style(file_obj.geoserver_layer_name)
        if not success:
            return 'error', message
        file_obj.processing_log += f'\n✓ Layer restyled: {message}'
        file_obj.save()
        return 'restyled', f'{style}: {message}'

    def style_after_repair(self, file_obj):
        """Restyle a repaired vector layer still in a generic style; what to add to its report line, or ''."""
        style = self.style_problem(file_obj)
        if style is None:
            return ''
        outcome, message = self.restyle(file_obj, style)
        return f'; {message}' if outcome == 'restyled' else f'; restyle failed: {message}'

    def layer_of(self, file_obj):
        resource = 'coverage' if gis_read.kind_of(file_obj.name) == 'raster' else 'featureType'
        store = file_obj.geoserver_datastore_name or file_obj.geoserver_layer_name
        return store, file_obj.geoserver_layer_name, resource

    def reprocess(self, file_obj):
        had_layer = bool(file_obj.geoserver_layer_name)
        file_obj.gis_status = 'processing'
        file_obj.processing_log = 'Starting GIS file processing (fix_gis_layers)...'
        file_obj.save()
        try:
            success, message = process_gis_file(file_obj)
        except gis_read.ShapefilePartsMissing as missing:
            success, message = False, str(missing)
        if file_obj.gis_status == 'drone_photo':
            file_obj.processing_log += f'\n✓ Processing completed: {message}'
            file_obj.save()
            return 'drone photo', message
        if not success:
            return self.fail(file_obj, 'Processing failed', message)
        file_obj.processing_log += f'\n✓ Processing completed: {message}'
        file_obj.save()
        problem = self.manager.layer_problem(*self.layer_of(file_obj)) if had_layer else 'not published'
        outcome, detail = self.repair(file_obj, problem)
        return ('reprocessed' if outcome != 'error' else outcome), f'{file_obj.crs}; {detail}'

    def repair(self, file_obj, problem):
        """Publish anew when GeoServer has no layer; else set what the layer lacks."""
        if problem in ('not published', 'missing'):
            file_obj.gis_status = 'processed'
            success, message, _ = self.manager.publish_file_to_geoserver(file_obj)
            if not success:
                return self.fail(file_obj, 'Publishing failed', message)
            return 'published', file_obj.geoserver_layer_name
        if problem is None:
            if file_obj.gis_status != 'published':
                file_obj.gis_status = 'published'
                file_obj.save()
            return 'ok', 'layer draws' + self.style_after_repair(file_obj)
        store, layer, resource = self.layer_of(file_obj)
        success, message = self.manager.ensure_layer_srs(store, layer, resource, epsg_of(file_obj.crs))
        if success and not self.manager.layer_draws(layer, file_obj.spatial_extent):
            success, message = False, f'GeoServer has {layer} but WMS cannot draw it'
        if not success:
            return self.fail(file_obj, 'Repair failed', message)
        file_obj.gis_status = 'published'
        file_obj.processing_log += f'\n✓ Layer repaired: {message}'
        file_obj.save()
        return 'repaired', f'{problem}: {message}' + self.style_after_repair(file_obj)

    def fail(self, file_obj, what, message):
        file_obj.gis_status = 'error'
        file_obj.processing_log += f'\n✗ {what}: {message}'
        file_obj.save()
        return 'error', message
