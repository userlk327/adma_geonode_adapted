from django import forms
from django.contrib import admin, messages
from django.contrib.admin.actions import delete_selected as django_delete_selected
from django.contrib.admin.utils import unquote
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.shortcuts import redirect
from django.urls import reverse
from . import adapt_share, permissions
from .models import Folder, File, Map, MapLayer, Tool, FolderGrant, RosterGroup, UserProfile

User = get_user_model()


class KeepAdaptRows:
    """Nothing on the ADAPT share is deleted here either: no ADAPT row, and no folder with one beneath it.

    The change page offers no Delete for these rows, and a bulk delete whose
    selection includes any of them deletes nothing and says why.
    """
    actions = ['delete_selected']

    def has_delete_permission(self, request, obj=None):
        if obj is not None and permissions.touches_adapt([obj]):
            return False
        return super().has_delete_permission(request, obj)

    @admin.action(permissions=['delete'], description=django_delete_selected.short_description)
    def delete_selected(self, request, queryset):
        if permissions.touches_adapt(queryset):
            self.message_user(
                request,
                f'Nothing was deleted: the selection includes ADAPT content. {permissions.ADAPT_DELETE_MESSAGE}',
                messages.ERROR,
            )
            return None
        return django_delete_selected(self, request, queryset)


@admin.register(Folder)
class FolderAdmin(KeepAdaptRows, admin.ModelAdmin):
    list_display = ['name', 'owner', 'parent', 'is_public', 'created_at']
    list_filter = ['is_public', 'created_at']
    search_fields = ['name']
    raw_id_fields = ['parent', 'owner']


@admin.register(File)
class FileAdmin(KeepAdaptRows, admin.ModelAdmin):
    list_display = ['name', 'owner', 'folder', 'file_type', 'file_size', 'is_public', 'created_at']
    list_filter = ['file_type', 'is_public', 'created_at']
    search_fields = ['name']
    raw_id_fields = ['folder', 'owner']
    readonly_fields = ['file_size', 'file_type', 'mime_type']


@admin.register(Map)
class MapAdmin(admin.ModelAdmin):
    list_display = ['name', 'owner', 'is_public', 'layer_count', 'created_at', 'updated_at']
    list_filter = ['is_public', 'created_at']
    search_fields = ['name', 'description']
    raw_id_fields = ['owner']
    readonly_fields = ['geoserver_layer_group_name', 'created_at', 'updated_at']


@admin.register(MapLayer)
class MapLayerAdmin(admin.ModelAdmin):
    list_display = ['file', 'map', 'layer_order', 'opacity', 'is_visible', 'added_at']
    list_filter = ['is_visible', 'added_at']
    search_fields = ['file__name', 'map__name']
    raw_id_fields = ['map', 'file']


@admin.register(Tool)
class ToolAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'category', 'status', 'is_system_tool', 'is_public', 'is_active', 'usage_count', 'version']
    list_filter = ['category', 'status', 'is_system_tool', 'is_public', 'is_active']
    search_fields = ['name', 'slug', 'description', 'short_description']
    raw_id_fields = ['owner']
    readonly_fields = ['id', 'usage_count', 'created_at', 'updated_at']
    prepopulated_fields = {'slug': ('name',)}
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'slug', 'short_description', 'description')
        }),
        ('Ownership & Visibility', {
            'fields': ('owner', 'is_system_tool', 'is_public', 'is_active')
        }),
        ('Categorization & Display', {
            'fields': ('category', 'status', 'icon', 'icon_color', 'version')
        }),
        ('Execution Configuration', {
            'fields': ('url_name', 'celery_task_name', 'input_config', 'output_config'),
            'classes': ('collapse',)
        }),
        ('Statistics & Metadata', {
            'fields': ('usage_count', 'id', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


class FolderGrantForm(forms.ModelForm):
    """Checks the path against the share and stores it as the share spells it."""

    class Meta:
        model = FolderGrant
        fields = ['path', 'user', 'group', 'can_write']

    def clean_path(self):
        try:
            path = adapt_share.canonical(self.cleaned_data.get('path') or '')
        except adapt_share.AdaptPathError as exc:
            raise forms.ValidationError(str(exc))
        if not adapt_share.is_folder(path):
            raise forms.ValidationError(f'"{path}" is a file, not a folder. Grants are given on folders.')
        return path


@admin.register(FolderGrant)
class FolderGrantAdmin(admin.ModelAdmin):
    form = FolderGrantForm
    list_display = ['folder_path', 'user', 'group', 'can_write', 'granted_by', 'created_at']
    list_filter = ['can_write', 'group']
    search_fields = ['path', 'user__username', 'group__name']
    list_select_related = ['user', 'group', 'granted_by']

    @admin.display(description='Folder', ordering='path')
    def folder_path(self, obj):
        return obj.path or '(whole share)'

    def save_model(self, request, obj, form, change):
        if not change:
            obj.granted_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(RosterGroup)
class RosterGroupAdmin(admin.ModelAdmin):
    """Groups adapt_users load keeps in step with the roster. Delete a row to hand a group back to the admins."""
    list_display = ['group', 'added_at']
    search_fields = ['group__name']
    readonly_fields = ['added_at']


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = 'Profile'


admin.site.unregister(User)


@admin.register(User)
class ProfileUserAdmin(UserAdmin):
    """Django's user admin, plus the profile (NUID) on the change page and in search.

    A user's Files and Folders are deleted with them, so a user who owns ADAPT
    rows (see permissions.owns_adapt) is not deleted, alone or in a bulk delete,
    until those rows are reassigned.
    """
    inlines = [UserProfileInline]
    actions = ['delete_selected']
    list_display = UserAdmin.list_display + ('nuid',)
    list_select_related = ['profile']
    search_fields = UserAdmin.search_fields + ('profile__nuid',)

    @admin.display(description='NUID', ordering='profile__nuid')
    def nuid(self, obj):
        profile = getattr(obj, 'profile', None)
        return profile.nuid if profile else None

    def delete_view(self, request, object_id, extra_context=None):
        user = self.get_object(request, unquote(object_id))
        if user is not None and self.has_delete_permission(request, user) and permissions.owns_adapt([user]):
            self.message_user(
                request, f'{user} was not deleted: they own ADAPT content. {permissions.ADAPT_OWNER_MESSAGE}',
                messages.ERROR,
            )
            opts = self.opts
            return redirect(reverse(
                f'admin:{opts.app_label}_{opts.model_name}_change', args=[user.pk], current_app=self.admin_site.name,
            ))
        return super().delete_view(request, object_id, extra_context)

    @admin.action(permissions=['delete'], description=django_delete_selected.short_description)
    def delete_selected(self, request, queryset):
        if permissions.owns_adapt(queryset):
            self.message_user(
                request,
                'Nothing was deleted: the selection includes a user who owns ADAPT content. '
                f'{permissions.ADAPT_OWNER_MESSAGE}',
                messages.ERROR,
            )
            return None
        return django_delete_selected(self, request, queryset)
