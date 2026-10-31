"""Regression: both SEC chunk ID layouts must agree in SQL and metadata filters."""
import json
import sqlite3
import unittest
from agent.filters import FilingFilter

class AccessionFilters(unittest.TestCase):
    def test_old_and_sectioned_ids_match_in_sql_and_metadata(self):
        accession='000032019323000106'
        identifiers=[accession+'#passage-000001',accession+'#item1A#passage-000001',
                     accession+'0#passage-000001','000032019324000106#item1A#passage-000001']
        predicate=FilingFilter(accession=accession)
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE passages(id TEXT, metadata TEXT)')
            for identifier in identifiers:
                db.execute('INSERT INTO passages VALUES (?,?)',(identifier,json.dumps({'ID':identifier})))
            clause,values=predicate.sql()
            actual=[row[0] for row in db.execute('SELECT id FROM passages WHERE '+clause,values)]
        self.assertEqual(actual,identifiers[:2])
        self.assertEqual([i for i in identifiers if predicate.matches({'ID':i})],identifiers[:2])
