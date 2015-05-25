from confu import Configurable

from storage import Storage
from gramps_xml_to_yaml import extract, transform, load


class WTFamilyETL(Configurable):
    needs = {
        'gramps_xml_path': str,
        'storage': Storage,
    }

    def import_gramps_xml(self):
        xml_root = extract(self.gramps_xml_path)
        items = transform(xml_root)
        for entity_name, pk, data in items:
            self.storage.add(entity_name, pk, data, upsert=True, commit=False)
        self.storage.commit()

    @property
    def commands(self):
        return [self.import_gramps_xml]