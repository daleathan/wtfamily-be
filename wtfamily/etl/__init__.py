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
import argh
from confu import Configurable
from pymongo import MongoClient

from .mongo_to_gramps_xml import export_to_xml
from .gramps_xml_to_mongo import import_from_xml


MONGO_DB_NAME = 'wtfamily-from-grampsxml'


class WTFamilyETL(Configurable):
    needs = {
        'gramps_xml_path': str,
        'mongo_client': MongoClient,
    }

    def import_gramps_xml(self, path=None, db_name=MONGO_DB_NAME,
                          replace=False):

        if db_name in self.mongo_client.database_names():
            if replace or argh.confirm('DROP and replace existing DB "{}"'
                                       .format(db_name)):
                self.mongo_client.drop_database(db_name)
            else:
                yield 'Not replacing the existing database.'
                return
        else:
            yield 'Importing into a new DB "{}"'.format(db_name)

        db = self.mongo_client[db_name]

        return import_from_xml(path or self.gramps_xml_path, db)

    def export_gramps_xml(self, path=None, db_name=MONGO_DB_NAME,
                          replace=False):
        db = self.mongo_client[db_name]

        # TODO: either compress and save to file,
        #       or remove the `path` and `replace` args
        return export_to_xml(db)

    @property
    def commands(self):
        return [
            self.import_gramps_xml,
            self.export_gramps_xml
        ]
