import re

from django import template
from django.utils import timezone
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe
from django.utils.timesince import timesince

register = template.Library()

# File.file_type values that are initialisms; the others read as words ('Spreadsheet').
TYPE_LABELS = {'gis': 'GIS', 'csv': 'CSV'}


@register.filter
def file_type_label(file_type):
    """A File.file_type as people read it: 'GIS', 'CSV', 'Image'.

    Usage: {{ file.file_type|file_type_label }}
    """
    if not file_type:
        return ''
    return TYPE_LABELS.get(file_type.lower(), file_type.title())


@register.filter
def ago(moment):
    """How long ago `moment` was, as "3 minutes ago", or "just now" under a minute.

    Usage: {{ file.created_at|ago }}
    """
    if not moment:
        return ''
    if (timezone.now() - moment).total_seconds() < 60:
        return 'just now'
    return f'{timesince(moment)} ago'


@register.filter(needs_autoescape=True)
def breakable(name, autoescape=True):
    """A file name that may wrap after its underscores and hyphens, and before a dot other than its extension's.

    A long name such as DJI_20250529121645_0001_MS_G.TIF has no spaces, so a
    narrow column would otherwise break it anywhere, or not at all and overflow.

    Usage: {{ file.name|breakable }}
    """
    escape = conditional_escape if autoescape else str
    parts = re.split(r'(?<=[_-])|(?=\.[^.]*\.)', str(name))
    return mark_safe('<wbr>'.join(escape(part) for part in parts if part))
