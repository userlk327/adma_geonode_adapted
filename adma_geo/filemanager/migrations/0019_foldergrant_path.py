"""Grants name a folder by its path on the ADAPT share instead of pointing at a Folder row.

ADMA stops keeping a Folder row for every directory on the share, so a grant
has to name its folder in a way that does not depend on one. Each grant's path
is the chain of folder names below the ADAPT root, as the old share sync copied them
from the share; a grant on the root itself becomes "", the whole share.
"""
from django.db import migrations, models
import django.db.models.deletion
from django.db.models.functions import Lower


def folder_path(folder):
    names = []
    while folder.parent_id is not None:
        names.append(folder.name)
        folder = folder.parent
    return '/'.join(reversed(names))


def copy_paths(apps, schema_editor):
    FolderGrant = apps.get_model('filemanager', 'FolderGrant')
    for grant in FolderGrant.objects.select_related('folder'):
        grant.path = folder_path(grant.folder)
        grant.save(update_fields=['path'])


def restore_folders(apps, schema_editor):
    """Point each grant back at its Folder row, found by name below the ADAPT root without case."""
    Folder = apps.get_model('filemanager', 'Folder')
    FolderGrant = apps.get_model('filemanager', 'FolderGrant')
    root = Folder.objects.filter(
        name='ADAPT', parent=None, is_third_party=True, third_party_source='adapt',
    ).first()
    for grant in FolderGrant.objects.all():
        folder = root
        for name in [part for part in grant.path.split('/') if part]:
            folder = folder and Folder.objects.filter(parent=folder, name__iexact=name).order_by('created_at').first()
        if folder is None:
            raise RuntimeError(f'Cannot undo: no ADAPT folder row for the grant on "{grant.path}"')
        grant.folder = folder
        grant.save(update_fields=['folder'])


class Migration(migrations.Migration):

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
        ('filemanager', '0018_roster_group'),
    ]

    operations = [
        migrations.AddField(
            model_name='foldergrant',
            name='path',
            field=models.CharField(blank=True, default='', help_text='Folder on the ADAPT share, e.g. "Soils/2024". Empty means the whole share.', max_length=1024),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name='foldergrant',
            name='folder',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='grants', to='filemanager.folder'),
        ),
        migrations.RunPython(copy_paths, restore_folders),
        migrations.RemoveConstraint(
            model_name='foldergrant',
            name='foldergrant_unique_user',
        ),
        migrations.RemoveConstraint(
            model_name='foldergrant',
            name='foldergrant_unique_group',
        ),
        migrations.RemoveField(
            model_name='foldergrant',
            name='folder',
        ),
        migrations.AlterModelOptions(
            name='foldergrant',
            options={'ordering': ['path']},
        ),
        migrations.AddConstraint(
            model_name='foldergrant',
            constraint=models.UniqueConstraint(Lower('path'), 'user', condition=models.Q(('user__isnull', False)), name='foldergrant_unique_user_path', violation_error_message='This user already has a grant on this folder. Edit that grant instead.'),
        ),
        migrations.AddConstraint(
            model_name='foldergrant',
            constraint=models.UniqueConstraint(Lower('path'), 'group', condition=models.Q(('group__isnull', False)), name='foldergrant_unique_group_path', violation_error_message='This group already has a grant on this folder. Edit that grant instead.'),
        ),
    ]
