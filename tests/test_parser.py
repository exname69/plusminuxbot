import unittest
from app.parser import parse_message, ParseError


class ParserTests(unittest.TestCase):
    def test_income(self):
        item = parse_message("+240 ногти")[0]
        self.assertEqual(item.amount_minor, 24000)
        self.assertEqual(item.comment, "ногти")

    def test_expense_without_comment(self):
        item = parse_message("-50")[0]
        self.assertEqual(item.amount_minor, -5000)
        self.assertEqual(item.comment, "")

    def test_decimal_yesterday_and_multiline(self):
        items = parse_message("вчера -1 200,50 аренда\n+100.25")
        self.assertEqual([i.amount_minor for i in items], [-120050, 10025])
        self.assertTrue(items[0].yesterday)
        self.assertFalse(items[1].yesterday)

    def test_reject_invalid_without_partial_success(self):
        with self.assertRaises(ParseError):
            parse_message("+100\nhello")
        for text in ["+0", "-0,00", "100", "+100.999", "-100foo"]:
            with self.subTest(text=text), self.assertRaises(ParseError):
                parse_message(text)

    def test_unicode_minus(self):
        self.assertEqual(parse_message("−30 материалы")[0].amount_minor, -3000)
