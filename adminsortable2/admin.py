import os
import json
from itertools import chain
from types import MethodType

from django.contrib import admin, messages
from django.contrib.contenttypes.forms import BaseGenericInlineFormSet
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured
from django.core.paginator import EmptyPage
from django.db import router, transaction
from django.db.models.aggregates import Max
from django.db.models.expressions import F
from django.db.models.functions import Coalesce
from django.db.models.signals import post_save, pre_save
from django.forms import widgets
from django.forms.fields import IntegerField
from django.forms.models import BaseInlineFormSet
from django.http import JsonResponse, HttpResponseNotAllowed, HttpResponseForbidden
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from django.urls import path, reverse

__all__ = ['SortableAdminMixin', 'SortableInlineAdminMixin']


def _get_default_ordering(model, model_admin):
    try:
        # first try with the model admin ordering
        _, prefix, field_name = model_admin.ordering[0].rpartition('-')
    except (AttributeError, IndexError, TypeError):
        pass
    else:
        return prefix, field_name

    try:
        # then try with the model ordering
        _, prefix, field_name = model._meta.ordering[0].rpartition('-')
    except (AttributeError, IndexError):
        raise ImproperlyConfigured(
            f"Model {model.__module__}.{model.__name__} requires a list or tuple 'ordering' in its Meta class"
        )
    else:
        return prefix, field_name


class MovePageActionForm(admin.helpers.ActionForm):
    step = IntegerField(
        required=False,
        initial=1,
        widget=widgets.NumberInput(attrs={'id': 'changelist-form-step'}),
        label=False
    )
    page = IntegerField(
        required=False,
        widget=widgets.NumberInput(attrs={'id': 'changelist-form-page'}),
        label=False
    )


class SortableAdminBase:
    @property
    def media(self):
        css = {'all': ['adminsortable2/css/sortable.css']}
        js = ['adminsortable2/js/adminsortable2.js']
        return super().media + widgets.Media(css=css, js=js)


class SortableAdminMixin(SortableAdminBase):
    BACK, FORWARD, FIRST, LAST, EXACT = range(5)
    action_form = MovePageActionForm

    @property
    def change_list_template(self):
        opts = self.model._meta
        app_label = opts.app_label
        return [
            os.path.join('adminsortable2', app_label, opts.model_name, 'change_list.html'),
            os.path.join('adminsortable2', app_label, 'change_list.html'),
            'adminsortable2/change_list.html'
        ]

    def __init__(self, model, admin_site):
        self.default_order_direction, self.default_order_field = _get_default_ordering(model, self)
        super().__init__(model, admin_site)
        self.enable_sorting = False
        self.order_by = None
        self._add_reorder_method()

    def get_list_display(self, request):
        list_display = list(super().get_list_display(request))
        try:
            index = list_display.index(self.default_order_field)
        except ValueError:
            list_display.insert(0, '_reorder_')
        else:
            list_display[index] = '_reorder_'
        return list_display

    def get_list_display_links(self, request, list_display):
        list_display_links = list(super().get_list_display_links(request, list_display))
        if self.default_order_field in list_display_links:
            list_display_links.remove(self.default_order_field)
            if not list_display_links:
                list_display_links = [list_display[0]]
        return list_display_links

    def _get_update_url_name(self):
        return f'{self.model._meta.app_label}_{self.model._meta.model_name}_sortable_update'

    def get_urls(self):
        my_urls = [
            path(
                'adminsortable2_update/',
                self.admin_site.admin_view(self.update_order),
                name=self._get_update_url_name()
            ),
        ]
        return my_urls + super().get_urls()

    def get_actions(self, request):
        actions = super().get_actions(request)
        qs = self.get_queryset(request)
        paginator = self.get_paginator(request, qs, self.list_per_page)
        if paginator.num_pages > 1 and 'all' not in request.GET and self.enable_sorting:
            # add actions for moving items to other pages
            move_actions = []
            cur_page = int(request.GET.get('p', 1))
            if cur_page > 1:
                move_actions.append('move_to_first_page')
            if cur_page > paginator.page_range[1]:
                move_actions.append('move_to_back_page')
            if cur_page < paginator.page_range[-2]:
                move_actions.append('move_to_forward_page')
            if cur_page < paginator.page_range[-1]:
                move_actions.append('move_to_last_page')
            if len(paginator.page_range) > 4:
                move_actions.append('move_to_exact_page')
            for fname in move_actions:
                actions.update({fname: self.get_action(fname)})
        return actions

    def get_changelist_instance(self, request):
        cl = super().get_changelist_instance(request)
        qs = self.get_queryset(request)
        _, order_direction, order_field = cl.get_ordering(request, qs)[0].rpartition('-')
        if order_field == self.default_order_field:
            self.enable_sorting = True
            self.order_by = f'{order_direction}{order_field}'
        else:
            self.enable_sorting = False
        return cl

    def _add_reorder_method(self):
        """
        Adds a bound method, named '_reorder_' to the current instance of
        this class, with attributes allow_tags, short_description and
        admin_order_field.
        This can only be done using a function, since it is not possible
        to add dynamic attributes to bound methods.
        """
        def func(this, item):
            if this.enable_sorting:
                order = getattr(item, this.default_order_field)
                html = '<div class="drag handle" pk="{0}" order="{1}">&nbsp;</div>'.format(item.pk, order)
            else:
                html = '<div class="drag">&nbsp;</div>'
            return mark_safe(html)

        # if the field used for ordering has a verbose name use it, otherwise default to "Sort"
        for order_field in self.model._meta.fields:
            if order_field.name == self.default_order_field:
                short_description = getattr(order_field, 'verbose_name', None)
                if short_description:
                    setattr(func, 'short_description', short_description)
                    break
        else:
            setattr(func, 'short_description', _("Sort"))
        setattr(func, 'admin_order_field', self.default_order_field)
        setattr(self, '_reorder_', MethodType(func, self))

    def update_order(self, request):
        if request.method != 'POST':
            return HttpResponseNotAllowed(f"Method {request.method} not allowed")
        if not self.has_change_permission(request):
            return HttpResponseForbidden('Missing permissions to perform this request')
        body = json.loads(request.body)
        startorder = int(body.get('startorder'))
        endorder = int(body.get('endorder', 0))
        moved_items = list(self._move_item(request, startorder, endorder))
        return JsonResponse(moved_items, safe=False)

    def save_model(self, request, obj, form, change):
        if not change:
            setattr(
                obj, self.default_order_field,
                self.get_max_order(request, obj) + 1
            )
        super().save_model(request, obj, form, change)

    def move_to_exact_page(self, request, queryset):
        self._bulk_move(request, queryset, self.EXACT)
    move_to_exact_page.short_description = _('Move selected to specific page')

    def move_to_back_page(self, request, queryset):
        self._bulk_move(request, queryset, self.BACK)
    move_to_back_page.short_description = _('Move selected ... pages back')

    def move_to_forward_page(self, request, queryset):
        self._bulk_move(request, queryset, self.FORWARD)
    move_to_forward_page.short_description = _('Move selected ... pages forward')

    def move_to_first_page(self, request, queryset):
        self._bulk_move(request, queryset, self.FIRST)
    move_to_first_page.short_description = _('Move selected to first page')

    def move_to_last_page(self, request, queryset):
        self._bulk_move(request, queryset, self.LAST)
    move_to_last_page.short_description = _('Move selected to last page')

    def _move_item(self, request, startorder, endorder):
        extra_model_filters = self.get_extra_model_filters(request)
        return self.move_item(startorder, endorder, extra_model_filters)

    def move_item(self, startorder, endorder, extra_model_filters=None):
        model = self.model
        rank_field = self.default_order_field

        if endorder < startorder:  # Drag up
            move_filter = {
                f'{rank_field}__gte': endorder,
                f'{rank_field}__lte': startorder - 1,
            }
            move_delta = +1
            order_by = f'-{rank_field}'
        elif endorder > startorder:  # Drag down
            move_filter = {
                f'{rank_field}__gte': startorder + 1,
                f'{rank_field}__lte': endorder,
            }
            move_delta = -1
            order_by = rank_field
        else:
            return model.objects.none()

        obj_filters = {rank_field: startorder}
        if extra_model_filters is not None:
            obj_filters.update(extra_model_filters)
            move_filter.update(extra_model_filters)

        with transaction.atomic():
            try:
                obj = model.objects.get(**obj_filters)
            except model.MultipleObjectsReturned:

                # noinspection PyProtectedMember
                raise model.MultipleObjectsReturned(
                    "Detected non-unique values in field '{rank_field}' used for sorting this model.\n"
                    "Consider to run \n    python manage.py reorder {model._meta.label}\n"
                    "to adjust this inconsistency."
                )

            move_qs = model.objects.filter(**move_filter).order_by(order_by)
            move_objs = list(move_qs)
            for instance in move_objs:
                setattr(
                    instance, rank_field,
                    getattr(instance, rank_field) + move_delta
                )
                # Do not run `instance.save()`, because it will be updated
                # later in bulk by `move_qs.update`.
                pre_save.send(
                    model,
                    instance=instance,
                    update_fields=[rank_field],
                    raw=False,
                    using=router.db_for_write(model, instance=instance),
                )
            move_qs.update(**{rank_field: F(rank_field) + move_delta})
            for instance in move_objs:
                post_save.send(
                    model,
                    instance=instance,
                    update_fields=[rank_field],
                    raw=False,
                    using=router.db_for_write(model, instance=instance),
                    created=False,
                )

            setattr(obj, rank_field, endorder)
            obj.save(update_fields=[rank_field])

        return [{
            'pk': instance.pk,
            'order': getattr(instance, rank_field)
        } for instance in chain(move_objs, [obj])]

    @staticmethod
    def get_extra_model_filters(request):
        """
        Returns additional fields to filter sortable objects
        """
        return {}

    def get_max_order(self, request, obj=None):
        return self.model.objects.aggregate(
            max_order=Coalesce(Max(self.default_order_field), 0)
        )['max_order']

    def _bulk_move(self, request, queryset, method):
        if not self.enable_sorting:
            return
        objects = self.model.objects.order_by(self.order_by)
        paginator = self.paginator(objects, self.list_per_page)
        current_page_number = int(request.GET.get('p', 1))

        if method == self.EXACT:
            page_number = int(request.POST.get('page', current_page_number))
            target_page_number = page_number
        elif method == self.BACK:
            step = int(request.POST.get('step', 1))
            target_page_number = current_page_number - step
        elif method == self.FORWARD:
            step = int(request.POST.get('step', 1))
            target_page_number = current_page_number + step
        elif method == self.FIRST:
            target_page_number = 1
        elif method == self.LAST:
            target_page_number = paginator.num_pages
        else:
            raise Exception('Invalid method')

        if target_page_number == current_page_number:
            # If you want the selected items to be moved to the start of the current page, then just do not return here
            return

        try:
            page = paginator.page(target_page_number)
        except EmptyPage as ex:
            self.message_user(request, str(ex), level=messages.ERROR)
            return

        queryset_size = queryset.count()
        page_size = page.end_index() - page.start_index() + 1
        endorders_step = -1 if self.order_by.startswith('-') else 1
        if queryset_size > page_size:
            # move objects to last and penultimate page
            endorders_end = getattr(objects[page.end_index() - 1], self.default_order_field) + endorders_step
            endorders = range(
                endorders_end - endorders_step * queryset_size,
                endorders_end,
                endorders_step
            )
        else:
            endorders_start = getattr(objects[page.start_index() - 1], self.default_order_field)
            endorders = range(
                endorders_start,
                endorders_start + endorders_step * queryset_size,
                endorders_step
            )

        if page.number > current_page_number:
            # Move forward
            queryset = queryset.reverse()
            endorders = reversed(endorders)

        for obj, endorder in zip(queryset, endorders):
            startorder = getattr(obj, self.default_order_field)
            self._move_item(request, startorder, endorder)

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['sortable_update_url'] = self.get_update_url(request)
        return super().changelist_view(request, extra_context)

    def get_update_url(self, request):
        """
        Returns a callback URL used for updating items via AJAX drag-n-drop
        """
        return reverse(f'{self.admin_site.name}:{self._get_update_url_name()}')

    def get_formset_kwargs(self, request, obj, inline, prefix):
        formset_params = super().get_formset_kwargs(request, obj, inline, prefix)
        if hasattr(inline, 'default_order_direction') and hasattr(inline, 'default_order_field'):
            formset_params.update(
                default_order_direction=inline.default_order_direction,
                default_order_field=inline.default_order_field,
            )
        return formset_params

    def get_inline_formsets(self, request, formsets, inline_instances, obj=None):
        inline_admin_formsets = super().get_inline_formsets(request, formsets, inline_instances, obj)
        for inline_admin_formset in inline_admin_formsets:
            if hasattr(inline_admin_formset.formset, 'default_order_direction'):
                classes = inline_admin_formset.classes.split()
                classes.append('sortable')
                if inline_admin_formset.formset.default_order_direction == '-':
                    classes.append('reversed')
                inline_admin_formset.classes = ' '.join(classes)
        return inline_admin_formsets


class PolymorphicSortableAdminMixin(SortableAdminMixin):
    """
    If the admin class is used for a polymorphic model, hence inherits from ``PolymorphicParentModelAdmin``
    rather than ``admin.ModelAdmin``, then additionally inherit from ``PolymorphicSortableAdminMixin``
    rather than ``SortableAdminMixin``.
    """
    def get_max_order(self, request, obj=None):
        return self.base_model.objects.aggregate(
            max_order=Coalesce(Max(self.default_order_field), 0)
        )['max_order']


class CustomInlineFormSetMixin:
    def __init__(self, default_order_direction=None, default_order_field=None, **kwargs):
        self.default_order_direction = default_order_direction
        self.default_order_field = default_order_field
        if default_order_field:
            if default_order_field not in self.form.base_fields:
                self.form.base_fields[default_order_field] = self.model._meta.get_field(default_order_field).formfield()

            order_field = self.form.base_fields[default_order_field]
            order_field.is_hidden = True
            order_field.required = False
            order_field.widget = widgets.HiddenInput(attrs={'class': '_reorder_'})

        super().__init__(**kwargs)

    def get_max_order(self):
        query_set = self.model.objects.filter(
            **{self.fk.get_attname(): self.instance.pk}
        )
        return query_set.aggregate(
            max_order=Coalesce(Max(self.default_order_field), 0)
        )['max_order']

    def save_new(self, form, commit=True):
        """
        New objects do not have a valid value in their ordering field.
        On object save, add an order bigger than all other order fields
        for the current parent_model.
        Strange behaviour when field has a default, this might be evaluated
        on new object and the value will be not None, but the default value.
        """
        obj = super().save_new(form, commit=False)

        order_field_value = getattr(obj, self.default_order_field, None)
        if order_field_value is None or order_field_value >= 0:
            max_order = self.get_max_order()
            setattr(obj, self.default_order_field, max_order + 1)
        if commit:
            obj.save()
        # form.save_m2m() can be called via the formset later on
        # if commit=False
        if commit and hasattr(form, 'save_m2m'):
            form.save_m2m()
        return obj


class CustomInlineFormSet(CustomInlineFormSetMixin, BaseInlineFormSet):
    pass


class SortableInlineAdminMixin(SortableAdminBase):
    formset = CustomInlineFormSet

    def __init__(self, parent_model, admin_site):
        self.default_order_direction, self.default_order_field = _get_default_ordering(self.model, self)
        super().__init__(parent_model, admin_site)


class CustomGenericInlineFormSet(CustomInlineFormSetMixin, BaseGenericInlineFormSet):
    def get_max_order(self):
        query_set = self.model.objects.filter(
            **{
                self.ct_fk_field.name: self.instance.pk,
                self.ct_field.name: ContentType.objects.get_for_model(
                    self.instance,
                    for_concrete_model=self.for_concrete_model
                )
            }
        )
        return query_set.aggregate(
            max_order=Coalesce(Max(self.default_order_field), 0)
        )['max_order']


class SortableGenericInlineAdminMixin(SortableInlineAdminMixin):
    formset = CustomGenericInlineFormSet
