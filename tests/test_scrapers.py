import unittest
from AutoBrand_Correction_In_Sheets import clean_brand_name


class TestBrandScrapers(unittest.TestCase):
    def test_clean_brand_name_strips_store_noise(self):
        self.assertEqual(clean_brand_name("Visit the Anker Store"), "Anker")
        self.assertEqual(clean_brand_name("Brand: Sony"), "Sony")
        self.assertEqual(clean_brand_name("Manufacturer: HOLGVE Shop."), "HOLGVE")
        self.assertEqual(clean_brand_name("Kiehl's Since 1851"), "Kiehl's")
        self.assertEqual(clean_brand_name(""), "N/A")


if __name__ == "__main__":
    unittest.main()
