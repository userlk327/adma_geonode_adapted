"""{% askadma_asset 'askadma/widget.js' %}: the static URL of an Ask ADMA file, with a hash of its content.

Ask ADMA's chat panel is a stylesheet and a script under static/askadma/, so pages stay light (Mohannad's
ADAPT folder pages are checked to stay under 150 KB) and browsers cache them. The hash in the address
makes browsers fetch the new copy after an update, without waiting for their cache to expire.
"""
import hashlib

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()
_urls = {}


@register.simple_tag
def askadma_asset(path):
    if path not in _urls:
        found = finders.find(path)
        digest = ""
        if found:
            with open(found, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()[:12]
        _urls[path] = static(path) + (f"?v={digest}" if digest else "")
    return _urls[path]
