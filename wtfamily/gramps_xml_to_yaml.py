#!/usr/bin/env python
"""
Experimental converter of compressed Gramps XML to YAML.

The resulting form is not defined yet.  It depends on the first results.
Perhaps something like a file per object in an entity-specific directory would
be fine.
"""
import datetime
import gzip
import uuid
from xml.etree import ElementTree

import argh
import blessings
from monk import *
import yaml


GRAMPS_NAMESPACE_LABEL = 'gramps'
NAMESPACES = {
    GRAMPS_NAMESPACE_LABEL: 'http://gramps-project.org/xml/1.6.0/',
}

def _with_namespace(field_name):
    return '{' + NAMESPACES[GRAMPS_NAMESPACE_LABEL] + '}' + field_name

def _strip_namespace(field_name):
    return field_name.replace('{' + NAMESPACES[GRAMPS_NAMESPACE_LABEL] + '}', '')

GRAMPS_ENTITIES = (
    'gramps:name-formats',
    'gramps:events',
    'gramps:people',
    'gramps:families',
    'gramps:sources',
    'gramps:places',
    'gramps:objects',
    'gramps:repositories',
#    'gramps:notes',
    'gramps:bookmarks',
    'gramps:namemaps',
)
SINGLE_VALUE_FIELDS = (
    # generic
    'id',
    'handle',
    'change',    # last changed timestamp
    'priv',      # is this a private record?

    # repositories
    'rname',

    # sources
    'stitle',
    'sauthor',
    'spubinfo',

    # notes
    'text',

    # namemaps
    'type',
    'key',
    'value',

    # objects
    'dateval',

    # places
    'ptitle',
    #'name',     # XXX it can be 1 for place but 1..N for person
    'coord',

    # people
    'gender',
)
# fields about which we are *sure* that they *can* have multiple values.
# in the future we may want a strict check and a field will have to belong
# to one of the two mappings.
MULTI_VALUE_FIELDS = (
    # repositories
    'url',
)
assert not any(x in SINGLE_VALUE_FIELDS for x in MULTI_VALUE_FIELDS)


LIST_OF_IDS = [str]                  # TODO use this (see above)
LIST_OF_HLINKS = [{'hlink': str}]    # TODO drop this (need ID manager first)
LIST_OF_HLINKS_WITH_ROLES = [
    {
        'hlink': str,       # TODO ID instead of hlink
        opt_key('role'): str,
    }
]
LIST_OF_URLS = [
    {
        'type': str,
        'href': str,
        opt_key('description'): str,
    },
]
ADDRESS = Anything()    # TODO it's a complex type
DATEVAL = {
    'val': str,
    opt_key('type'): one_of(['after', 'before']),
}
DATESPAN = {
    'start': str,
    'stop': str,
}
ATTRIBUTE = {
    'type': str,
    'value': str,
    opt_key('citationref'): LIST_OF_HLINKS,
}
BASE_ENTITY = {
    'id': str,
    'handle': str,
}
SCHEMATA = {
    'gramps:places': {
        'name': [str],    # TODO: str  (single value)
        opt_key('ptitle'): str,
        opt_key('coord'): {'long': str, 'lat': str},
        opt_key('alt_name'): [str],
        'change': datetime.datetime,
        'type': str,
        opt_key('priv'): str,    # TODO: True/False
        opt_key('url'): LIST_OF_URLS,

        opt_key('placeref'): [
            {
                'hlink': str,
                opt_key('dateval'): [ DATEVAL ],
                opt_key('datespan'): [ DATESPAN ],
            },
        ],
        opt_key('citationref'): LIST_OF_HLINKS,  # TODO LIST_OF_IDS
        opt_key('noteref'): LIST_OF_HLINKS,      # TODO LIST_OF_IDS
    },
    'gramps:people': {
        'name': [
            IsA(str) | {
                'type': str,
                opt_key('first'): str,
                opt_key('surname'): [
                    IsA(str) | {
                        'text': str,
                        opt_key('derivation'): str,
                        opt_key('prim'): str,       # TODO True/False (primary? flag)
                    },
                ],
                opt_key('nick'): str,
                opt_key('citationref'): LIST_OF_HLINKS,
                opt_key('priv'): str,     # TODO bool
                opt_key('alt'): str,     # TODO bool
                opt_key('group'): str,    # group as...
                opt_key('dateval'): [ DATEVAL ],
            },
        ],
        'gender': one_of(['M', 'F']),

        opt_key('childof'): LIST_OF_HLINKS,
        opt_key('parentin'): LIST_OF_HLINKS,

        opt_key('url'): LIST_OF_URLS,
        opt_key('priv'): str,    # TODO bool
        opt_key('url'): LIST_OF_URLS,
        opt_key('address'): [ ADDRESS ],

        'change': datetime.datetime,

        opt_key('objref'): [
            {
                'hlink': str,
                opt_key('region'): [
                    {
                        'corner1_y': str,
                        'corner2_y': str,
                        'corner1_x': str,
                        'corner2_x': str,
                    },
                ],
            },
        ],
        opt_key('eventref'): [
            {
                'hlink': str,
                opt_key('role'): str,
                opt_key('attribute'): [ ATTRIBUTE ],
            },
        ],
        opt_key('noteref'): LIST_OF_HLINKS,               # TODO LIST_OF_IDS_WITH_ROLES
        opt_key('citationref'): LIST_OF_HLINKS,           # TODO LIST_OF_IDS_WITH_ROLES
        opt_key('personref'): [
            {
                'rel': str,    # напр., "крёстный отец"
                'hlink': str,
                opt_key('citationref'): LIST_OF_HLINKS,
            }
        ],
        opt_key('attribute'): [ ATTRIBUTE ],
    },
}
for k in SCHEMATA:
    SCHEMATA[k].update(BASE_ENTITY)


t = blessings.Terminal()


def extract(path):
    print('extract...')
    with gzip.open(path) as f:
        root = ElementTree.fromstring(f.read())
    #print(root.tag)
    #print(root.attrib)
    #print(list(root))
    return root


def transform(xml_root):
    print('transform...')
    converter = Converter()
    for entity_name in GRAMPS_ENTITIES:
        schema = SCHEMATA.get(entity_name)
        nodes = xml_root.findall(entity_name + '/', NAMESPACES)
        for node in nodes:
            pk, item = converter(node, entity_name=entity_name)
            if schema:
                try:
                    validate(schema, item)
                except ValidationError as e:
                    print(entity_name, pk, item)
                    raise e from None
            yield entity_name, pk, item


def load(items, out):
    print('load...')
    full_data = {}
    for entity_name, pk, data in items:
        full_data.setdefault(entity_name, {})[pk] = data

    with open(out, 'w') as f:
        yield yaml.dump(full_data, f, allow_unicode=True, default_flow_style=False)
    print('Wrote YAML to {}'.format(t.yellow(out)))


def main(path='/tmp/all.gramps', out='/tmp/all.gramps.yaml'):
    xml_root = extract(path)
    items = transform(xml_root)
    for x in load(items, out):
        pass


GLOBAL_FIELD_PROCESSORS = {
    'change': lambda v: datetime.datetime.utcfromtimestamp(int(v)) if v else None,
}

# XXX challenges:
# - forward-looking hlink→ID resolver
#   - perhaps as a final step, outside of the converter?
#     it would just register the mappings on an external visitor object)
#     - we'll also need to go *into* the items.  They should be flat-ish, i.e.
#       each item is a dictionary where values can be single or multiple.
#       This is defined by schema.
#       So we just iterate the list if the field isn't in SINGLE_VALUE_FIELDS.
# - some entities (e.g. namemaps) don't have IDs
#   - currently using UUID but not sure if it's ok
class Converter:
    def __init__(self):
        self._handle_to_id = {}

    def __call__(self, node, entity_name=None):
        item = {}
        for k, v in self._get_fields_from_node(node, entity_name):
            transform = GLOBAL_FIELD_PROCESSORS.get(k)
            if transform:
                v = transform(v)

            if k in SINGLE_VALUE_FIELDS:
                assert k not in item
                item[k] = v
            else:
                item.setdefault(k, []).append(v)


        id = node.attrib.get('id', str(uuid.uuid1()))    # if there's no id, assign a UUID
        handle = node.attrib.get('handle', id)           # if there's no handle, use ID
        self._alias_handle_to_id(handle, id)

        return id, item

    def _get_fields_from_node(self, entity_node, entity_name):
        """
        :param entity_node: XML tag representing a single entity of given type.
            It can have nested nodes (entity properties/fields) which, in turn,
            can include further levels.

        Simple (single-value) fields are mapped to key/value pairs.
        The values are plain (e.g. a string).

        Complex (nested/multi-value) fields are mapped to key/value pairs
        where the key is the unique name of the fields and the value is a
        list of values this fields takes.  For example, this XML::

            <my_entity attr1="1">
                <foo albatross="2"/>
                <foo albatross="3"/>
                <bar bar_attr="4">abc</bar>
                <quux quux_attr="5">
                    <bingo>Hello</bingo>
                </quux>
            </my_entity>

        ...may map to native Python data structures this way::

            # in a list of "my_entity" items:
            {
                'attr1': '1',
                'foo': [
                    {'albatross': '2'},
                    {'albatross': '3'},
                ],
                'bar': [
                    { 'bar_attr': '4', 'text': 'abc', }
                ],
                'quux': [
                    {
                        'quux_attr': '5',
                        'bingo': ['Hello'],    # or {'text': 'Hello'}
                    }
                ]

            }

        The details may depend on implementation.  See schema.
        """
        for k, v in entity_node.attrib.items():
            yield k, v

        for field_node in entity_node:
            #print(t.blue('  {}  "{}"   {}'.format(child.tag, (child.text or '').strip(), child.attrib)))
            field_name = _strip_namespace(field_node.tag)

            assert not (field_node.attrib and (field_node.text.strip() if field_node.text else None))
            value = field_node.attrib.copy() or field_node.text


            if len(field_node):
                # XXX tag-specific logic
                if entity_name == 'gramps:people' and field_name == 'name':
                    if isinstance(value, str):
                        value = {'text': value}
                    for subfield in field_node:
                        nested_field = _strip_namespace(subfield.tag)

                        plaintext_fields = 'first', 'nick', 'group'

                        complex_fields = 'surname', 'citationref', 'dateval'

                        if nested_field in plaintext_fields:
                            value[nested_field] = subfield.text
                        elif nested_field in complex_fields:
                            subdata = subfield.attrib.copy()
                            if subfield.text:
                                subdata.update(text=subfield.text)
                            value.setdefault(nested_field, []).append(subdata)
                        else:
                            assert 0, 'not sure what to do with '+ subfield.tag + ', should test'
                else:
                    if isinstance(value, str):
                        value = {'text': value}
                    for subfield in field_node:
                        subfield_shortname = _strip_namespace(subfield.tag)
                        subdata = subfield.attrib.copy()
                        value.setdefault(subfield_shortname, []).append(subdata)
                # XXX / tag-specific logic

            yield field_name, value


    def _alias_handle_to_id(self, handle, id):
        assert handle not in self._handle_to_id
        self._handle_to_id[handle] = id


if __name__ == '__main__':
    argh.dispatch_command(main)
