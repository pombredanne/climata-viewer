from wq.db.contrib.dbio.models import IoModel
from wq.db.contrib.dbio.signals import import_complete, new_metadata
from wq.db.contrib.vera.models import (
    BaseEvent, Result, create_eventresult_model
)
from wq.db.patterns import models
from wq.db.rest.models import get_object_id, get_ct
from wq.io.util import flattened

from django.dispatch import receiver
from django.core.cache import cache
from django.utils.timezone import now


from rest_framework.settings import import_from_string
import datetime


DEFAULT_OPTIONS = (
    'start_date',
    'end_date',
    'state',
    'county',
    'basin',
    'station',
    'parameter',
)


class Webservice(models.Model):
    name = models.CharField(max_length=255)
    homepage = models.URLField()
    authority = models.ForeignKey('identify.Authority')
    class_name = models.CharField(max_length=255)

    def __unicode__(self):
        return self.name

    @property
    def io_class(self):
        return import_from_string(self.class_name, self.name)

    def describe_option(self, option, name=None):
        return {
            'name': name,
            'ignored': option.ignored,
            'required': option.required,
            'multi': option.multi,
        }

    @property
    def default_options(self):
        options = {}
        io_options = self.io_class.get_filter_options()
        for name in DEFAULT_OPTIONS:
            if name not in io_options:
                raise Exception(
                    "%s is missing %s option!"
                    % (self.class_name, name)
                )
            options[name] = self.describe_option(io_options[name], name)
        return options

    @property
    def extra_options(self):
        options = []
        for name, option in self.io_class.get_filter_options().items():
            if name in DEFAULT_OPTIONS:
                continue
            options.append(self.describe_option(option, name))
        return options


class DataRequest(IoModel):
    user = models.ForeignKey('auth.User', null=True, blank=True)
    webservice = models.ForeignKey(Webservice)
    requested = models.DateTimeField(auto_now_add=True)
    completed = models.DateTimeField(null=True, blank=True)
    deleted = models.DateTimeField(null=True, blank=True)
    public = models.BooleanField(default=False)

    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)

    def load_io(self):
        key = 'req_%s' % self.pk
        loaded_io = cache.get(key)
        if loaded_io:
            return loaded_io
        options = self.get_io_options()
        loaded_io = flattened(self.webservice.io_class, **options)
        cache.set(key, loaded_io, 60 * 60 * 2)
        return loaded_io

    def get_io_options(self):
        opt_values = {
            'debug': True,
        }
        for field in DEFAULT_OPTIONS:
            opt_values[field] = getattr(self, field)
        return opt_values

    def get_object_id(self, obj):
        # Try to use the same identifier that the webservice uses for
        # this parameter/site/etc.
        if get_ct(obj).is_identified:
            idents = obj.identifiers.filter(
                authority=self.webservice.authority
            )
        else:
            idents = []

        if len(idents) > 0:
            return idents[0].slug
        else:
            # No authority-specific IDs found, use default ID for object
            return get_object_id(obj)

    def get_id_choices(self, model, meta):
        # Assume new site codes are always new sites
        from locations.models import Site
        if model == Site:
            lat = meta.get('latitude', None)
            lng = meta.get('longitude', None)
            if lat and lng:
                return model.objects.near(float(lat), float(lng))
            return model.objects.none()
        return model.objects.all()

    _labels = {}
    def __unicode__(self):
        if not self.webservice_id:
            return None
        if self.pk in DataRequest._labels:
            return DataRequest._labels[self.pk]

        param_filters = self.get_filter_labels('parameter')
        if param_filters:
            params = ", ".join(param_filters)
        else:
            params = "All Data"

        loc_filters = []
        for name in ('state', 'county', 'basin', 'station'):
            if getattr(self, name):
                name = 'site' if name == 'station' else name
                loc_filters += self.get_filter_labels(name)

        if loc_filters:
            locs = " for %s" % ", ".join(loc_filters)
        else:
            locs = ""

        if self.start_date and self.end_date:
            date = " from %s to %s" % (self.start_date, self.end_date)
        elif self.start_date:
            date = " since %s" % self.start_date
        elif self.end_date:
            date = " until %s" % self.end_date
        else:
            date = ""

        label = params + locs + date
        DataRequest._labels[self.pk] = label
        return label

    _python = {}

    @property
    def as_python(self):
        if self.pk in DataRequest._python:
            return DataRequest._python[self.pk]
        class_path = self.webservice.class_name.split(".")
        module = ".".join(class_path[:-1])
        class_name = class_path[-1]
        args = ""
        opts = self.get_io_options()
        for opt in DEFAULT_OPTIONS:
            if opts[opt] is not None:
                val = opts[opt]
                if isinstance(val, list) and len(val) == 1:
                    val = val[0]
                if not isinstance(val, list):
                    val = '"%s"' % val
                args += "\n    {key}={val},".format(key=opt, val=val)

        if self.webservice.io_class.nested:
            loop = "for series in data:\n    print series\n"
            loop += "    for row in series.data:\n        print row"
        else:
            loop = "for row in data:\n    print row"

        tmpl = "from {module} import {class_name}\n\n"
        tmpl += "data = {class_name}({args}\n)\n\n{loop}\n"
        python = tmpl.format(
            module=module,
            class_name=class_name,
            args=args,
            loop=loop
        )
        DataRequest._python[self.pk] = python
        return python

    def get_filter_rels(self, name):
        rels = self.inverserelationships.filter(from_content_type__name=name)
        if not rels.count():
            return None
        return rels

    def get_filter_ids(self, name):
        rels = self.get_filter_rels(name)
        if rels:
            return [self.get_object_id(rel.right) for rel in rels]

    def get_filter_labels(self, name):
        rels = self.get_filter_rels(name)
        if rels:
            return [rel.right_dict['item_label'] for rel in rels]

    @property
    def state(self):
        opts = self.webservice.default_options
        if self.county and not opts['state']['required']:
            # County implies state for most climata IOs
            return None
        return self.get_filter_ids('state')

    @property
    def county(self):
        return self.get_filter_ids('county')

    @property
    def basin(self):
        return self.get_filter_ids('basin')

    @property
    def station(self):
        return self.get_filter_ids('site')

    @property
    def parameter(self):
        return self.get_filter_ids('parameter')

    @property
    def project(self):
        projects = self.get_filter_rels('project')
        if projects:
            return projects[0].right
        return None

    class Meta:
        ordering = ("-requested",)


class Project(models.IdentifiedRelatedModel):
    user = models.ForeignKey('auth.User', null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    deleted = models.DateTimeField(null=True, blank=True)
    public = models.BooleanField(default=False)

    @property
    def requests(self):
        return (rel.right for rel in self.relationships.all())

    @property
    def has_data(self):
        return any(req.completed for req in self.requests)


class Event(BaseEvent):
    type = models.CharField(max_length=10, null=True, blank=True)
    date = models.DateField()

    def __unicode__(self):
        return "%s on %s%s" % (
            self.site, self.date, "(%s)" % self.type if self.type else ""
        )

    class Meta:
        unique_together = ("site", "date", "type")
        ordering = ('-date', 'type')


EventResult = create_eventresult_model(Event, Result)


@receiver(import_complete)
def on_import_complete(sender, instance=None, status=None, **kwargs):
    if not instance:
        return
    instance.completed = now()
    instance.save()


@receiver(new_metadata)
def on_new_metadata(sender, instance=None, object=None, identifier=None,
                    **kwargs):
    if not instance or not object or not identifier:
        return
    identifier.authority = instance.webservice.authority
    identifier.save()

    # Assume parameters with units are numeric
    if getattr(object, 'units', None):
        object.is_numeric = True
        object.save()
