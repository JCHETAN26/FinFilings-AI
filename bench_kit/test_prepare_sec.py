import io
import json
import unittest
from prepare_sec import Passages


class PreparationTest(unittest.TestCase):
    def parse(self, source, feed_size):
        output = io.StringIO()
        parser = Passages(output, 'AAPL', '320193', 2023, 'accession', 3)
        for i in range(0, len(source), feed_size):
            parser.feed(source[i:i + feed_size])
        parser.close()
        parser.emit()
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_stream_boundaries_hidden_markup_and_no_duplication(self):
        source = ('<html><head><title>hidden</title></head><body>'
                  'revenue growth<br/>cash &amp; debt '
                  '<ix:hidden>fabricated hidden text</ix:hidden>'
                  '<span style="display: none">hidden text</span>'
                  'risk <b>factors</b> ending</body></html>')
        expected = self.parse(source, len(source))
        for size in (1, 7, 13, 65536):
            self.assertEqual(self.parse(source, size), expected)
        text = ' '.join(row['Text'] for row in expected)
        self.assertEqual(text, 'revenue growth cash & debt risk factors ending')
        self.assertEqual(len({row['ID'] for row in expected}), len(expected))
        self.assertTrue(all(row['SectionItem'] == 'Full filing (item not classified)' for row in expected))


if __name__ == '__main__':
    unittest.main()
