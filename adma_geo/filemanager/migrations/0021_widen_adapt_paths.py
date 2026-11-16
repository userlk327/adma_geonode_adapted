"""Room for whole paths on the ADAPT share, and an index to find a file's record by its path.

An ADAPT record is keyed by its path on the share. At 255 characters, two long
paths that agree that far were cut to the same identity. 1024 is the width
FolderGrant.path already has.
"""
from django.db import migrations, models
import django.db.models.functions.text


class Migration(migrations.Migration):

    dependencies = [
        ('filemanager', '0020_adapt_index'),
    ]

    operations = [
        migrations.AlterField(
            model_name='file',
            name='third_party_id',
            field=models.CharField(blank=True, help_text='Unique identifier from the third-party platform', max_length=1024, null=True),
        ),
        migrations.AlterField(
            model_name='file',
            name='third_party_url',
            field=models.URLField(blank=True, help_text='URL to the file on the third-party platform', max_length=1024, null=True),
        ),
        migrations.AlterField(
            model_name='folder',
            name='third_party_id',
            field=models.CharField(blank=True, help_text='Unique identifier from the third-party platform', max_length=1024, null=True),
        ),
        migrations.AddIndex(
            model_name='file',
            index=models.Index(django.db.models.functions.text.Upper('third_party_id'), condition=models.Q(('third_party_source', 'adapt')), name='file_adapt_path_idx'),
        ),
    ]
