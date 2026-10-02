import unittest

import okpd_check


class OkpdCheckTests(unittest.TestCase):
    def setUp(self):
        if okpd_check.reference() is None:
            self.skipTest("нет models/okpd2_reference.json.gz")

    def test_existing_code_untouched(self):
        self.assertEqual(okpd_check.check("32.50.13.110", "Катетер"), ("32.50.13.110", None))

    def test_replaced_class_restored(self):
        # предзащита: класс подменён на 02
        self.assertEqual(okpd_check.check("02.50.13.110", "Проводник для доступа к сосудам")[0], "32.50.13.110")
        self.assertEqual(okpd_check.check("02.21.10.120", "Предварительный медицинский осмотр")[0], "86.21.10.120")
        self.assertEqual(okpd_check.check("02.51.52.140", "Кефир")[0], "10.51.52.140")
        self.assertEqual(okpd_check.check("02.39.18", "Оливки б/к")[0], "10.39.18")  # короткий код

    def test_unrecoverable_code_left_as_is(self):
        self.assertEqual(okpd_check.check("02.99.99.999", "Нечто"), ("02.99.99.999", None))
        self.assertEqual(okpd_check.check("абв", "Нечто"), ("абв", None))

    def test_fix_item_keeps_original_and_summary(self):
        row = {"product_name": "Кефир", "okpd2_code": " 02,51,52,140 "}
        fix = okpd_check.fix_item(row)
        self.assertEqual((row["okpd2_code"], row["okpd2_original"]), ("10.51.52.140", "02.51.52.140"))
        text = okpd_check.summary([fix] * 5, 10, 2)
        self.assertIn("5 из 10", text[0])
        self.assertIn("класс «02»", text[0])
        self.assertIn("у 2 позиций", text[1])
