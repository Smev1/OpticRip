import unittest

from opticrip import registration


class RegistrationTests(unittest.TestCase):
    def test_generated_keys_validate(self):
        key = registration.make_key("alice@example.com")
        self.assertTrue(registration.validate(key)[0])
        self.assertEqual(registration.validate(key)[1], key)

    def test_tampered_key_is_invalid(self):
        key = registration.make_key("bob")
        broken = key[:-1] + ("0" if key[-1] != "0" else "1")
        self.assertFalse(registration.validate(broken)[0])

    def test_bogus_key_is_invalid(self):
        self.assertFalse(registration.validate("BOGUS123")[0])
        self.assertFalse(registration.validate("")[0])

    def test_key_extracted_from_email(self):
        key = registration.make_key("carol")
        body = f"Hello,\n\nYour key is {key}\n\nThanks."
        valid, extracted = registration.validate(body)
        self.assertTrue(valid)
        self.assertEqual(extracted, key)

    def test_key_hash_is_stable(self):
        key = registration.make_key("dave")
        self.assertEqual(registration.key_hash(key), registration.key_hash(key))
        self.assertNotEqual(registration.key_hash(key), key)


if __name__ == "__main__":
    unittest.main()
