#    WTFamily is a genealogical software.
#
#    Copyright © 2014—2018  Andrey Mikhaylenko
#
#    This file is part of WTFamily.
#
#    WTFamily is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Lesser General Public License as published
#    by the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    WTFamily is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Lesser General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public License
#    along with WTFamily.  If not, see <http://gnu.org/licenses/>.
import datetime
import functools
import itertools
import re

from cached_property import cached_property
from flask import g
from dateutil.parser import parse as parse_date
import geopy.distance

from schema import *


RELATED_KEY_PREFIX = 'related_'


class ObjectNotFound(Exception):
    pass


def as_list(f):
    @functools.wraps(f)
    def inner(*args, **kwargs):
        retval = f(*args, **kwargs)
        return list(retval)
    return inner


def icount(iterable):
    return sum(1 for _ in iterable)


class Entity:
    entity_name = NotImplemented
    sort_key = None
    schema = COMMON_SCHEMA

    REFERENCES = NotImplemented

    # let them access the exception class by Entity (sub)class attribute
    ObjectNotFound = ObjectNotFound

    def __init__(self, data):
        self._data = data

        # XXX degrades performance, don't use in production
        if __debug__:
            self.validate()

    def __eq__(self, other):
        if type(self) == type(other) and self.id == other.id:
            return True
        else:
            return False

    def __hash__(self):
        # for `set([p1, p2])` etc.
        return hash('{} {}'.format(type(self), self.id))

    @classmethod
    def _get_database(cls):
        "This can be monkey-patched to avoid Flask's `g`"

        return g.mongo_db

    @classmethod
    def _get_collection(cls):
        database = cls._get_database()

        return database[cls.entity_name]

    def _find_refs(self, key, model):
        # 'eventref.id' is fine for MongoDB lookups, but not for `__getitem__`.
        # We just strip the inner part here, it will be conditionally tried
        # anyway by `_extract_refs()` below.
        if key.endswith('.id'):
            key = key.partition('.id')[0]

        try:
            refs = self._data[key]
        except KeyError:
            return []

        try:
            pks = _extract_refs(refs)
        except KeyError:
            print('malformed refs?', refs)
            raise

        if not isinstance(pks, list):
            pks = [pks]

        return model.find({
            'id': {
                '$in': pks
            }
        })

    def find_related(self, other_cls, by_key=None):
        """
        Returns instances of given model referenced by current instance.

        If `key` is not specified, it is inferred from `self.REFERENCES`.
        Note that you may _have_ to specify a key if there's more than one
        key containing references to given other model.
        """
        if not by_key:
            by_key = self.REFERENCES[other_cls.__name__]
        return self._find_refs(by_key, other_cls)

    @classmethod
    def find_all_referencing(cls, other_cls_or_obj, other_id=None):
        """
        Returns instances of this model that reference given another model
        or model instance.  Usage::

            # having an instance
            place = Place({'id': 123})
            related = Event.find_all_referencing(place)

            # without an instance
            related = Event.find_all_referencing(Place, 123)
        """
        if isinstance(other_cls_or_obj, Entity):
            other_instance = other_cls_or_obj
            other_cls = other_instance.__class__
            other_id = other_instance.id
        else:
            other_cls = other_cls_or_obj
            assert isinstance(other_cls, type)
            assert other_id

        assert issubclass(other_cls, Entity)
        key = cls.REFERENCES[other_cls.__name__]

        return cls.find({key: other_id})

    @classmethod
    def get(cls, pk):
        instance = cls.find_one({'id': pk})
        if not instance:
            raise cls.ObjectNotFound(pk)

        return instance

    @classmethod
    def find(cls, conditions=None):
        for item in cls._get_collection().find(conditions):
            try:
                yield cls(item)
            except ValidationError as e:
                import sys
                import pprint
                sys.stderr.write('ERROR in {.__name__}:\n{}\n'
                                 .format(cls, pprint.pformat(item)))
                raise e

    @classmethod
    def find_one(cls, conditions=None):
        item = cls._get_collection().find_one(conditions)
        if item:
            return cls(item)

    # FIXME optimize! this is insane.
    @classmethod
    def find_by_event_ref(cls, pk):
        # anything except Event can reference to an Event
        return cls.find({
            'eventref': pk
        })
        for obj in cls.find():
            refs = obj._data.get('eventref')
            if not refs:
                continue
            if pk in _extract_refs(refs):
                yield obj

    @classmethod
    def aggregate(cls, conditions, *related_models):
        """
        TODO: separate aggregation from base query:

            Event.find(id=x).aggregate(Person, Place)

        Prerequisite: all Model.find_* methods return ResultSet instances
        which provide additional methods.  If that's applicable here...

        A possibly better syntax:

            Event.find(id=x) \
                .aggregate(Person, field='personref') \
                .aggregate(Place, local_field='place')

        TODO: custom aliases, e.g.:

              Person.find_one(id=x).aggregate({
                  parents: {'model': Person, 'localField': ...},
                  children: {'model': Person, 'localField': ...},
              })

              # ^^^ this case is actually more complicated because these
              # relations are established through families, so we need more
              # items in the pipeline.
        """
        pipeline_stages = []

        for related_model in related_models:
            assert issubclass(related_model, Entity)

            if related_model == cls:
                continue

            # there can be two cases:
            # 1) central model references another one → that one gets aggregated;
            # 2) another model references central one → still gets aggregated.

            local_key = cls.REFERENCES.get(related_model.__name__, 'id')
            foreign_key = related_model.REFERENCES.get(cls.__name__, 'id')

            if not local_key and not foreign_key:
                raise Exception('Could not find a reference between {} and {}'
                                .format(cls.__name__ and related_model.__name__))

            assert local_key != foreign_key, (cls, related_model, local_key, foreign_key)

            lookup = {
                'from': related_model.entity_name,
                'as': RELATED_KEY_PREFIX + related_model.entity_name,
                'localField': local_key,
                'foreignField': foreign_key
            }

            pipeline_stages.append({
                '$match': conditions,
            })
            pipeline_stages.append({
                '$lookup': lookup
            })

        print(pipeline_stages)

        for item in cls._get_collection().aggregate(pipeline_stages):
            temp_extracted_keys = {}

            for related_model in related_models:
                key = RELATED_KEY_PREFIX + related_model.entity_name
                sub_items = item.pop(key)
                temp_extracted_keys[key] = [related_model(x) for x in
                                            sub_items]

            obj = cls(item)

            obj._data = dict(obj._data, **temp_extracted_keys)

            yield obj

    @classmethod
    def count(cls):
        return cls._get_collection().count()

    @property
    def _id(self):
        return self._data['_id']

    @property
    def id(self):
        return self._data['id']

    @property
    def handle(self):
        return self._data['handle']

    def validate(self):
        try:
            validate(self.schema, self._data)
        except ValidationError as e:
            import pprint
            pprint.pprint(self.schema)
            pprint.pprint(self._data)
            raise e from None

    @property
    def sortkey(self):
        return self.id

    @property
    def is_private(self):
        return self._data.get('priv', False)

    def get_public_data(self, protect=True):
        """
        Object data in a simplified and predictable format.
        No string-or-list-of-dicts and such nonsense.
        Main purpose: to display the item without extra logic in the View.
        """
        data = self.get_pretty_data()
        def _protect_value(value):
            if protect:
                if isinstance(value, str):
                    return '[private]'
                else:
                    return None
            else:
                if isinstance(value, str):
                    return '[private] {}'.format(value)
                else:
                    return value

        if self.is_private:
            return dict((k, _protect_value(v)) for k,v in data.items())
        else:
            return data

    def get_pretty_data(self):
        related_keys = [k for k in self._data.keys() if
                        k.startswith(RELATED_KEY_PREFIX)]
        related_data = {}
        for key in related_keys:
            #related_data[key] = [_strip_objectid(x) for x in self._data[key]]
            related_data[key] = [x.get_pretty_data() for x in self._data[key]]

        return dict(related_data, **self._get_pretty_data())

    def _get_pretty_data(self):
        raise NotImplementedError

    def matches_query(self, query):
        raise NotImplementedError

    def save(self):
        self.validate()
        self._get_collection().insert_one(self._data)
        #self._get_collection().replace_one({id: self.id}, self._data,
        #                                   upsert=True)


class Family(Entity):
    entity_name = 'families'
    schema = FAMILY_SCHEMA
    REFERENCES = {
        'Event': 'events.id',
    }

    def __repr__(self):
        return '{} + {}'.format(self.father or '?',
                                self.mother or '?')

    def _get_participant(self, key):
        try:
            pk = self._data[key]['id']
        except KeyError:
            return None
        else:
            return Person.find_one({'id': pk})

    def _get_pretty_data(self):
        return {
            # TODO use foo_id for IDs
            'father': self._data.get('father'),
            'mother': self._data.get('mother'),
            'citation_ids': _simplified_refs(self._data.get('citationref')),
            'note_ids': self._data.get('noteref'),
            'child_ids': _simplified_refs(self._data.get('childref')),
            'event_ids': _simplified_refs(self._data.get('events')),
            'attributes': self._data.get('attribute'),
        }

    @property
    def father(self):
        return self._get_participant('father')

    @property
    def mother(self):
        return self._get_participant('mother')

    @property
    def children(self):
        return self.find_related(Person, 'childref')

    @property
    def events(self):
        return self.find_related(Event)

    @property
    def people(self):
        if self.father:
            yield self.father
        if self.mother:
            yield self.mother
        for child in self.children:
            yield child

    def get_partner_for(self, person):
        assert person in (self.father, self.mother)
        if person != self.father:
            return self.father
        else:
            return self.mother

    @property
    def sortkey(self):
        if self.father:
            return '{}#{}'.format(self.father.group_name, self.father.name)
        if self.mother:
            return '{}#{}'.format(self.mother.group_name, self.mother.name)
        return self.id


class Person(Entity):
    entity_name = 'people'
    schema = PERSON_SCHEMA
    REFERENCES = {
        'Citation': 'citationref.id',
        'Event': 'eventref.id',
        'MediaObject': 'objectref.id',
    }
    NAME_TEMPLATE = '{first} {patronymic} {primary} ({nonpatronymic})'

    # these are for templates, etc.
    GENDER_MALE = 'M'
    GENDER_FEMALE = 'F'

    def __repr__(self):
        #return 'Person {} {}'.format(self.name, self._data)
        return '{}'.format(self.name)

    def _format_all_names(self, template=NAME_TEMPLATE):
        names = self._data['name']
        return self._format_names(names, template) or []

    def _format_one_name(self, template=NAME_TEMPLATE):
        return self._format_all_names(template)[0]

    def _get_pretty_data(self):
        return {
            # TODO use foo_id for IDs
            'group_names': list(self.group_names),
            'group_name': self.group_name,
            'names': self.names,
            'name': self.name,
            'first_and_last_names': self.first_and_last_names,
            'initials': self.initials,
            'gender': self.gender,
            'birth': str(self.birth),
            'death': str(self.death),
            'age': self.age,
            'attributes': self.attributes,
            'child_in_families': _simplified_refs(self._data.get('childof')),
            'parent_in_families': _simplified_refs(self._data.get('parentin')),
            'citation_ids': _simplified_refs(self._data.get('citationref')),
            'note_ids': _simplified_refs(self._data.get('noteref')),
            'event_ids': _simplified_refs(self._data.get('eventref')),
        }

    @property
    def names(self):
        return self._format_all_names()

    @property
    def name(self):
        return self._format_one_name()

    @property
    def first_and_last_names(self):
        return self._format_one_name('{first} {primary}')

    @property
    def first_name(self):
        return self._format_one_name('{first}')

    @property
    def initials(self):
        return ''.join(x[0].upper() for x in self.name.split(' ') if x)

    @property
    def group_names(self):
        name_nodes = self._data['name']
        if not isinstance(name_nodes, list):
            name_nodes = [name_nodes]

        # Check for name groups and aliases; if none, use first found surname
        first_found_surname = None
        for n in name_nodes:
            assert not isinstance(n, str)
            if 'group' in n:
                yield n['group']

            _, primary_surnames, _, _ = self._get_name_parts(n)
            for surname in primary_surnames:
                if not first_found_surname:
                    first_found_surname = surname
                alias = NameMap.group_as(surname)
                if alias:
                    yield alias
        if first_found_surname:
            yield first_found_surname

    @property
    def group_name(self):
        for name in self.group_names:
            # first found wins
            return name
        else:
            return self.name

    @cached_property
    @as_list
    def events(self):
        # TODO: the `eventref` records are dicts with `hlink` and `role`.
        #       we need to somehow decorate(?) the yielded event with these roles.
        items = self.find_related(Event)
        return sorted(items, key=lambda e: e.date)

    @cached_property
    @as_list
    def places(self):
        # unique with respect to the original order (expecting events sorted by date)
        places = []
        seen = {}
        for event in self.events:
            if event.place and event.place.id not in seen:
                places.append(event.place)
                seen[event.place.id] = True
        return places

    @property
    def attributes(self):
        try:
            attribs = self._data['attribute']
        except KeyError:
            return []
        return attribs

    @property
    def citations(self):
        return self.find_related(Citation)

    @property
    def media(self):
        return self.find_related(MediaObject)

    def get_parent_families(self):
        return self.find_related(Family, by_key='childof')

    def get_families(self):
        return self.find_related(Family, by_key='parentin')

    def get_parents(self):
        for family in self.get_parent_families():
            if family.mother:
                yield family.mother
            if family.father:
                yield family.father

    def get_siblings(self):
        for family in self.get_parent_families():
            for child in family.children:
                if child != self:
                    yield child

    def get_partners(self):
        for family in self.get_families():
            partners = family.father, family.mother
            for partner in partners:
                if partner and partner != self:
                    yield partner

    def get_children(self):
        for family in self.get_families():
            for child in family.children:
                yield child

    def find_ancestors(self):
        stack = [self]
        while stack:
            p = stack.pop()
            yield p
            for parent in p.get_parents():
                stack.append(parent)

    def find_descendants(self):
        stack = [self]
        while stack:
            p = stack.pop()
            yield p
            for child in p.get_children():
                stack.append(child)

    @cached_property
    @as_list
    def related_people(self):
        parents = list(self.get_parents())
        siblings = list(self.get_siblings())
        partners = list(self.get_partners())
        children = list(self.get_children())
        people = parents + siblings + partners + children
        seen = {}
        for person in people:
            if person.id not in seen:
                yield person
                seen[person.id] = True

    @property
    def birth(self):
        for event in self.events:
            if event.type == Event.TYPE_BIRTH:
                return event.date
        return DateRepresenter()

    @property
    def death(self):
        for event in self.events:
            if event.type == Event.TYPE_DEATH:
                return event.date
        return DateRepresenter()

    @property
    def age(self):
        if not self.birth:
            return

        try:
            if self.death:
                return self.death.year - self.birth.year
            else:
                return datetime.date.today().year - self.birth.year
        except TypeError:
            # probably one of the dates was `datestr` (could not be parsed)
            return

    @property
    def gender(self):
        return self._data['gender']

    @property
    def is_male(self):
        return self.gender == self.GENDER_MALE

    @property
    def is_female(self):
        return self.gender == self.GENDER_FEMALE

    @classmethod
    def _get_name_parts(cls, name_node):

        first = name_node.get('first', '?')
        primary_surnames = []
        patronymic = []
        nonpatronymic = []

        surname_spec = name_node.get('surname', '?')

        if isinstance(surname_spec, str):
            surname_spec = [
                {
                    'text': surname_spec,
                }
            ]

        if isinstance(surname_spec, dict):
            surname_spec = [surname_spec]

        for surname in surname_spec:
            #print('surname:', surname)
            if isinstance(surname, str):
                surname = {
                    'text': surname,
                }
            derivation = surname.get('derivation')
            is_primary = surname.get('prim') != '0'
            text = surname.get('text', '???')
            if derivation == 'Patronymic':
                patronymic.append(text)
            elif is_primary:
                primary_surnames.append(text)
            else:
                nonpatronymic.append(text)

        return first, primary_surnames, patronymic, nonpatronymic

    @classmethod
    def _format_names(cls, name_node, template=NAME_TEMPLATE):
        if not isinstance(name_node, list):
            name_node = [name_node]
        return [cls._format_name(n, template=template) for n in name_node]

    @classmethod
    def _format_name(cls, name_node, template=NAME_TEMPLATE):
        #template = '{primary} ({nonpatronymic}), {first} {patronymic}'

        first, primary_surnames, patronymic, nonpatronymic = cls._get_name_parts(name_node)

        return template.format(
            first = first,
            primary = ', '.join(primary_surnames),
            patronymic = ' '.join(patronymic),
            nonpatronymic = ', '.join(nonpatronymic),
        ).replace(' ()', '').strip()

    def matches_query(self, query):
        patterns = query.lower().split()
        raw_tokens = itertools.chain(self.names, self.group_names)
        tokens = [t.lower().strip() for t in raw_tokens]
        return all(any(p in t.lower() for t in tokens) for p in patterns)


def _format_dateval(dateval):
    if not dateval:
        return
    if 'val' in dateval:
        val = dateval['val']
    elif 'daterange' in dateval:
        assert 0, dateval
        val = '{}—{}'.format(
            dateval['daterange'].get('start'),
            dateval['daterange'].get('stop')
        )
    else:
        val = '?'
    vals = [
        dateval.get('quality'),
        dateval.get('type'),
        val,
    ]
    return ' '.join([x for x in vals if x])


class Event(Entity):
    entity_name = 'events'
    sort_key = lambda item: item.date
    schema = EVENT_SCHEMA
    REFERENCES = {
        'Place': 'place.id',
        'Citation': 'citationref.id',
    }

    TYPE_BIRTH = 'Birth'
    TYPE_DEATH = 'Death'

    def __repr__(self):
        #return 'Event {}'.format(self._data)
        return '{0.date} {0.type} {0.summary} {0.place}'.format(self)

    def _get_pretty_data(self):
        first_place_ref = _simplified_refs(self._data.get('place'))
        if isinstance(first_place_ref, list):
            first_place_ref = first_place_ref[0]

        return {
            # TODO use foo_id for IDs
            'type': self.type,
            'date': str(self.date),
            'date_year': str(self.date.year),
            'summary': self.summary,
            'place_id': first_place_ref,
            'citation_ids': _simplified_refs(self._data.get('citationref')),
        }

    @property
    def type(self):
        return self._data['type']

    @property
    def date(self):
        date = self._data.get('date')
        if date:
            return DateRepresenter(**date)
        else:
            # XXX this is a hack for `xs|sort(attribute='x')` Jinja filter
            # in Python 3.x environment where None can't be compared
            # to anything (i.e. TypeError is raised).
            return DateRepresenter()

    @property
    def summary(self):
        return self._data.get('description', '')

    @property
    def place(self):
        refs = list(self.find_related(Place))
        if refs:
            assert len(refs) == 1
            return refs[0]

    @property
    def people(self):
        return Person.find_all_referencing(self)

    @property
    def families(self):
        return Family.find_all_referencing(self)

    @property
    def citations(self):
        return self.find_related(Citation)


class Place(Entity):
    entity_name = 'places'
    REFERENCES = {
        'Citation': 'citationref.id',
        'Place': 'placeref.id'
    }
    schema = PLACE_SCHEMA

    def __repr__(self):
        return '{0.name}'.format(self)

    def _get_pretty_data(self):
        return {
            # TODO use foo_id for IDs
            'name': self.name,
            'other_names': self.alt_names,
            'coords': self.coords,
            'parent_place_ids': _simplified_refs(self._data.get('placeref')),
            'citation_ids': _simplified_refs(self._data.get('citationref')),
            'note_ids': _simplified_refs(self._data.get('noteref')),
        }

    def matches_query(self, query):
        patterns = query.lower().split()
        return all(any(p in n.lower() for n in self.names) for p in patterns)

    @property
    def name(self):
        return self.names[0]

    @property
    def names(self):
        return [x['value'] for x in self.names_annotated]

    @property
    def names_annotated(self):
        names = self._data.get('pname', [])
        assert names, 'id={} : {}'.format(self.id, self._data)
        return names

    @property
    def title(self):
        return self._data.get('ptitle')

    @property
    def alt_names(self):
        return self.names[1:]

    @property
    def coords(self):
        coords = self._data.get('coord')
        if not coords:
            return
        return {
            'lat': _normalize_coords_to_pure_degrees(coords['lat']),
            'lng': _normalize_coords_to_pure_degrees(coords['long']),
        }

    @property
    def coords_tuple(self):
        coords = self.coords
        if not coords:
            return
        return coords['lat'], coords['lng']

    def distance_to(self, other):
        if not (self.coords and other.coords):
            return
        return geopy.distance.vincenty(self.coords_tuple, other.coords_tuple)

    @property
    def parent_places(self):
        return self.find_related(Place)

    @cached_property
    @as_list
    def nested_places(self):
        return self.find_all_referencing(self)

    @cached_property
    @as_list
    def events(self):
        return Event.find_all_referencing(self)

    @cached_property
    def events_years(self):
        dates = sorted(e.date for e in self.events if e.date)
        if not dates:
            return 'years unknown'
        since = min(dates)
        until = max(dates)
        if since == until:
            return since
        else:
            return '{.year}—{.year}'.format(since, until)

    @cached_property
    @as_list
    def events_recursive(self):

        # build a list of refs for current place hierarchy
        places = []
        nested_to_see = [self]
        while nested_to_see:
            place = nested_to_see.pop()
            places.append(place)
            nested_to_see.extend(place.nested_places)

        events_seen = {}

        # find events with references to any of these places
        for place in places:
            for event in Event.find_all_referencing(place):
                if event.id in events_seen:
                    continue
                yield event
                events_seen[event.id] = True

    @cached_property
    @as_list
    def people(self):
        people = {}
        for event in self.events:
            for person in event.people:
                people.setdefault(person.id, person)
            for family in event.families:
                for person in family.people:
                    people.setdefault(person.id, person)
        return people.values()

#    @classmethod
#    def find(cls, conditions=None):
#        for elem in cls._find(conditions):
#            # exclude orphaned events (e.g. if the person was skipped
#            # on export for whatever reason)
#            if list(elem.people):
#                yield elem


class Source(Entity):
    entity_name = 'sources'
    schema = SOURCE_SCHEMA
    sort_key = lambda item: item.title

    def __repr__(self):
        return str(self.title)

    def _get_pretty_data(self):
        return {
            # TODO use foo_id for IDs
            'title': self.title,
            'author': self.author,
            'pubinfo': self.pubinfo,
            'abbrev': self.abbrev,
            'repository': _simplified_refs(self._data.get('reporef')),
            'note_ids': _simplified_refs(self._data.get('noteref')),
        }

    def matches_query(self, query):
        patterns = query.lower().split()
        raw_tokens = self.title, self.author, self.pubinfo
        tokens = [t.lower() for t in raw_tokens if t]
        return all(any(p in t for t in tokens) for p in patterns)

    @property
    def title(self):
        return self._data.get('stitle')

    @property
    def author(self):
        return self._data.get('sauthor')

    @property
    def pubinfo(self):
        return self._data.get('spubinfo')

    @property
    def abbrev(self):
        return self._data.get('sabbrev')

    @property
    def citations(self):
        return Citation.find_all_referencing(self)

    @property
    def repository(self):
        return self._find_refs('reporef', Repository)


class Citation(Entity):
    entity_name = 'citations'
    schema = CITATION_SCHEMA

    REFERENCES = {
        'Source': 'sourceref.id',
        'Note': 'noteref.id',
        'MediaObject': 'objref.id',
    }

    def __repr__(self):
        if self.page:
            return self.page
        return str(self.id)

    def _get_pretty_data(self):
        return {
            'page': self.page,
            'date': str(self.date),
            'source': _simplified_refs(self._data.get('sourceref')),
            'note_ids': _simplified_refs(self._data.get('noteref')),
            'media_ids': _simplified_refs(self._data.get('objref')),
        }

    @property
    def source(self):
        refs = list(self._find_refs('sourceref', Source))
        if refs:
            assert len(refs) == 1
            return refs[0]

    @property
    def page(self):
        return self._data.get('page')

    @property
    def date(self):
        date = self._data.get('date')
        if date:
            return DateRepresenter(**date)
        else:
            # XXX this is a hack for `xs|sort(attribute='x')` Jinja filter
            # in Python 3.x environment where None can't be compared
            # to anything (i.e. TypeError is raised).
            return DateRepresenter()

    @property
    def notes(self):
        return self.find_related(Note)

    @property
    def events(self):
        return Event.find_all_referencing(self)

    @property
    def people(self):
        return Person.find_all_referencing(self)

    @property
    def media(self):
        return self.find_related(MediaObject)


class Note(Entity):
    entity_name = 'notes'
    schema = NOTE_SCHEMA
    REFERENCES = {
        'MediaObject': 'objref.id',
    }

    def _get_pretty_data(self):
        return {
            # TODO use foo_id for IDs
            'text': self.text,
            'type': self.type,
            'media': _simplified_refs(self._data.get('objref')),
        }

    @property
    def text(self):
        return self._data['text']

    @property
    def type(self):
        return self._data['type']

    @property
    def media(self):
        return self.find_related(MediaObject)


class Bookmark(Entity):
    entity_name = 'bookmarks'
    schema = BOOKMARK_SCHEMA


class NameMap(Entity):
    entity_name = 'namemaps'
    schema = NAME_MAP_SCHEMA

    TYPE_GROUP_AS = 'group_as'

    _cache_by_group_as = {}

    def __repr__(self):
        return '<{} "{}" → "{}">'.format(self.type, self.key, self.value)

    def _get_pretty_data(self):
        return {
            'type': self.type,
            'key': self.key,
            'value': self.value,
        }

    @property
    def type(self):
        return self._data.get('type')

    @property
    def key(self):
        return self._data.get('key')

    @property
    def value(self):
        return self._data.get('value')

    @classmethod
    def group_as(self, key):
        # XXX optimization for in-memory store.
        # This was the original (slow) version:
        #
        #   for item in self.find():
        #       if item.type == self.TYPE_GROUP_AS and item.key == key:
        #           return item.value
        #
        if not self._cache_by_group_as:
            for item in self.find():
                if item.type == self.TYPE_GROUP_AS:
                    self._cache_by_group_as[item.key] = item.value
        try:
            return self._cache_by_group_as[key]
        except KeyError:
            return None


class NameFormat(Entity):
    entity_name = 'name-formats'
    schema = NAME_FORMAT_SCHEMA


class MediaObject(Entity):
    entity_name = 'objects'
    schema = MEDIA_OBJECT_SCHEMA

    @property
    def src(self):
        return self._data['file']['src']

    @property
    def description(self):
        return self._data['file']['description']

    @property
    def mime(self):
        return self._data['file']['mime']

    @property
    def date(self):
        value = self._data.get('date')
        if value:
            return DateRepresenter(**value)
        else:
            return ''

    def __repr__(self):
        return '<{0.description} ({0.date}): {0.mime} {0.src}>'.format(self)


class Repository(Entity):
    entity_name = 'repositories'
    schema = REPOSITORY_SCHEMA

    def __repr__(self):
        return '<Repository {type} {rname}>'.format(**self._data)


def _extract_refs(ref):
    """
    Returns a list of IDs (strings)
    """
    if isinstance(ref, str):
        # 'foo'
        return [ref]
    if isinstance(ref, dict):
        # {'hlink': 'foo'}
        return [ref['id']]

    # [{'hlink': 'foo'}]
    assert isinstance(ref, list)

    return [x['id'] if isinstance(x, dict) else x for x in ref]

def _extract_ids(obj, key):
    value = obj._data.get(key)
    if not value:
        return []
    return _extract_refs(value)

def _format_date(obj_data):
    date = obj_data.get('date')
    if date:
        return str(DateRepresenter(**date))
    return ''

def _normalize_coords_to_pure_degrees(coords):
    if isinstance(coords, float):
        return coords
    assert isinstance(coords, str)
    parts = [float(x) for x in re.findall('([0-9\.]+)', coords)]
    pure_degrees = parts.pop(0)
    if parts:
        # minutes
        pure_degrees += parts.pop(0) / 60
    if parts:
        # seconds
        pure_degrees += parts.pop(0) / (60*60)
    assert not parts, (coords, pure_degrees, parts)
    if 'S' in coords or 'W' in coords:
        pure_degrees = -pure_degrees
    return pure_degrees


class DateRepresenter:
    """
    A simple alternative to Gramps' gen.lib.date.Date object.

    Supported properties: modifier, quality.

    Unsupported: alternate calendars; newyear start date.

    Examples::

        DateRepresenter()      # unknown/undefined date
        DateRepresenter('1919')

    """
    MOD_NONE = None
    MOD_BEFORE = 'before'
    MOD_AFTER = 'after'
    MOD_ABOUT = 'about'
    MOD_RANGE = 'range'
    MOD_SPAN = 'span'
    MOD_TEXTONLY = 'textonly'
    MODIFIER_OPTIONS = (MOD_NONE, MOD_BEFORE, MOD_AFTER, MOD_ABOUT, MOD_RANGE,
                        MOD_SPAN, MOD_TEXTONLY)
    COMPOUND_MODIFIERS = MOD_RANGE, MOD_SPAN

    QUAL_NONE = None
    QUAL_ESTIMATED = 'estimated'
    QUAL_CALCULATED = 'calculated'
    QUALITY_OPTIONS = (QUAL_NONE, QUAL_ESTIMATED, QUAL_CALCULATED)

    def __init__(self, value=None, modifier=MOD_NONE, quality=QUAL_NONE):
        assert modifier in self.MODIFIER_OPTIONS
        assert quality in self.QUALITY_OPTIONS

        # TODO: validate the arguments

        self.value = value
        self.modifier = modifier
        self.quality = quality

    def __bool__(self):
        return self.value is not None

    def __str__(self):
        if self.value is None:
            return '?'
        return self._format(self.value)

    def _format(self, value):
        def _shorten_stop_subvalue(v):
            """
            >>> _shorten_stop_subvalue({'start': '1882', 'stop': '1895'})
            {'start': '1882', 'stop': '95'}
            >>> _shorten_stop_subvalue({'start': '1882-01', 'stop': '1895-05'})
            {'start': '1882-01', 'stop': '1895-05'}
            >>> _shorten_stop_subvalue({'start': '1882', 'stop': '1903'})
            {'start': '1882', 'stop': '1903'}
            """
            if 'start' not in v or 'stop' not in v:
                return v
            start = v['start']
            stop = v['stop']
            is_year_granularity = len(start) == len(stop) == 4
            if is_year_granularity and stop.startswith(start[:2]):
                stop = stop[2:]
            return dict(value, start=start, stop=stop)

        formats = {
            self.MOD_NONE: '{}',
            self.MOD_TEXTONLY: '{}',
            self.MOD_SPAN: '{0[start]}..{0[stop]}',
            self.MOD_RANGE: '[{0[start]}-{0[stop]}]',
            self.MOD_BEFORE: '<{}',
            self.MOD_AFTER: '>{}',
            self.MOD_ABOUT: '≈{}',
        }
        template = formats[self.modifier]
        value = _shorten_stop_subvalue(value)
        val = template.format(value)

        quality_abbrevs = {
            self.QUAL_ESTIMATED: 'est',
            self.QUAL_CALCULATED: 'calc',
            self.QUAL_NONE: '',
        }

        vals = [
            quality_abbrevs[self.quality],
            #self.modifier,    # excluded here because it's in the val's template
            val,
        ]
        return ' '.join([x for x in vals if x])

    def __eq__(self, other):
        if isinstance(other, type(self)) and str(self) == str(other):
            return True

    def __lt__(self, other):
        assert isinstance(other, type(self));
        # FIXME this is extremely rough
        return str(self.year) < str(other.year)

    def _can_be_parsed(self):
        return self.modifier != self.MOD_TEXTONLY

    @property
    def century(self):
        year = str(self.year)
        if not year:
            return '?'
        return '{}xx'.format(year[:2])

    @property
    def year(self):
        "Returns earliest known year as string"

        if not self._can_be_parsed():
            return ''

        if isinstance(self.value, str):
            value = self.value
        elif self.modifier in self.COMPOUND_MODIFIERS:
            value = self.value.get('start')
        else:
            return ''

        return self._parse_to_year(value)

    @property
    def year_formatted(self):
        if not self._can_be_parsed():
            return ''

        if self.is_compound:
            start, stop = self.boundaries
            # match the structure expected by template
            value = {
                'start': self._parse_to_year(start) if start else '',
                'stop':  self._parse_to_year(stop)  if stop  else '',
            }
        else:
            value = self.year
        return self._format(value)

    @property
    def is_estimated(self):
        return self.quality == self.QUAL_ESTIMATED

    @property
    def is_compound(self):
        return self.modifier in self.COMPOUND_MODIFIERS

    @property
    def is_approximate(self):
        return self.is_estimated or self.modifier == self.MOD_RANGE

    @property
    def boundaries(self):
        assert self.is_compound
        return self.value.get('start'), self.value.get('stop')

    @property
    def delta(self):
        if self.modifier == self.MOD_SPAN:
            start, stop = (self._parse_to_datetime(x) for x in self.boundaries)
            assert start and stop
            # TODO format properly for humans
            return stop - start

    @property
    def earliest_datetime(self):
        if not self._can_be_parsed():
            return ''

        if self.is_compound:
            value = self.boundaries[0]
        else:
            value = self.value
        return self._parse_to_datetime(value)

    @property
    def latest_datetime(self):
        if not self._can_be_parsed():
            return ''

        if self.is_compound:
            value = self.boundaries[-1]
        else:
            value = self.value
        return self._parse_to_datetime(value)

    def delta_compared(self, other):
        return self.earliest_datetime - other.latest_datetime

    def _parse_to_datetime(self, value):
        if isinstance(value, int):
            return datetime.datetime(value)
        if not isinstance(value, str):
            raise TypeError('expected a str, got {!r}'.format(value))
        # supplying default to avoid bug when the default day (31) was out
        # of range for given month (e.g. 30th is the last possible DoM).
        #print('    parse_date(', repr(value),')')
        return parse_date(value, default=datetime.datetime(1,1,1))

    def _parse_to_year(self, value):
        return self._parse_to_datetime(value).year


def _simplified_refs(value):
    """
    Normalizes Gramps references to a predictable form without metadata.

    >>> _simplified_refs(None)
    None
    >>> _simplified_refs('X0001')
    'X0001'
    >>> _simplified_refs({'id': 'X0001'})
    'X0001'
    >>> _simplified_refs(['X0001', 'X0002'])
    ['X0001', 'X0002']
    >>> _simplified_refs([{'id': 'X0001'}, 'X0002')
    ['X0001', 'X0002']
    """
    if not value:
        return
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value['id']
    if isinstance(value, list):
        return [x['id'] if isinstance(x, dict) else x for x in value]

## TODO: recursive?
#def _strip_objectid(value):
#    assert isinstance(value, dict), value
#
#    clean = dict(value)
#    clean.pop('_id')
#
#    print(clean)
#
#    return clean
