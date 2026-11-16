from django import template

from filemanager.permissions import FolderCounts

register = template.Library()


@register.filter
def counts_for(folder, user):
    """The counts `user` may see inside `folder`, as permissions.FolderCounts.

    Usage: {% with counts=folder|counts_for:user %}{{ counts.summary }}{% endwith %}
    A page that shows the same folder twice (list and panel views) reuses one result.
    """
    cache = folder.__dict__.setdefault('_adma_counts', {})
    key = getattr(user, 'pk', None)
    if key not in cache:
        cache[key] = FolderCounts(user, folder)
    return cache[key]
